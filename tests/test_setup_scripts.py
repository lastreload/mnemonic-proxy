"""Testa setup.sh e run-strata-proxy.sh (a camada de um clique, sem tocar no motor).

Cobre os tres pontos da revisao:
  1. o fluxo funciona sem Python 3.14: o config criado nasce com kv_archive coerente com
     o venv escolhido, e um config pessoal que pede kv_archive sem suporte faz o script
     falhar com mensagem clara sem ser sobrescrito;
  2. a checagem do motor aceita so 2xx e distingue conexao/timeout de HTTP 4xx/5xx;
  3. nenhum rm -rf em caminho arbitrario: VENV_MAIN / VENV_KV fora do projeto, com "..",
     symlink, ou pasta que nao e venv, sao recusados antes de qualquer remocao.

Os testes usam venvs reais criados por `python -m venv` (rapido) e um stub de
`mnemonic-proxy` que nao sobe servidor: o que se testa e a casca, nao o proxy.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, cwd, env=None, timeout=180):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def make_tree():
    """copia o baseline rastreado (pyproject, ctxproxy, examples) + os scripts atuais."""
    tmp = tempfile.mkdtemp(prefix="mp-shell-")
    r = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git archive falhou: " + r.stderr.decode()[:200])
    subprocess.run(["tar", "-x", "-C", tmp], input=r.stdout, check=True)
    for name in ("setup.sh", "run-strata-proxy.sh"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(tmp, name))
    return tmp


def stub_venv(tree, name, kv=False):
    """venv com bin/python (real) e bin/mnemonic-proxy (stub que nao sobe servidor).

    kv=True embrulha o python: `import compression.zstd` passa, o resto e o python real.
    """
    d = os.path.join(tree, name)
    binp = os.path.join(d, "bin")
    os.makedirs(binp, exist_ok=True)
    real = sys.executable
    if kv:
        wrap = os.path.join(binp, "python")
        with open(wrap, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n"
                     "if [ \"$1\" = \"-c\" ]; then case \"$2\" in *compression.zstd*) exit 0;; esac; fi\n"
                     "exec " + real + " \"$@\"\n")
        os.chmod(wrap, 0o755)
    else:
        os.symlink(real, os.path.join(binp, "python"))
    stub = os.path.join(binp, "mnemonic-proxy")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"[stub mnemonic-proxy] $*\"\n")
    os.chmod(stub, 0o755)
    return name


def tmp_abs(tree, name):
    return os.path.join(tree, name)


def strata_dir(tree, slot_path):
    """pasta do 'Strata' com um JSON de modelo que declara slot_save_path."""
    d = os.path.join(tree, "strata")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "modelo.json"), "w", encoding="utf-8") as fh:
        json.dump({"slot_save_path": slot_path}, fh)
    return d


def strata_configs(tree, name, items):
    """pasta do 'Strata' com varios JSON de motor: items = [(model, tokenizer, port, slot)].

    Simula a instalação com mais de um pack: cada config aponta o tokenizer e o
    slot do proprio pack, e o lancador tem de escolher o arquivo do motor em uso,
    nao o primeiro em ordem alfabetica.
    """
    d = os.path.join(tree, name)
    os.makedirs(d, exist_ok=True)
    for model, tok, port, slot in items:
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(d, "strata-%s.json" % model), "w", encoding="utf-8") as fh:
            json.dump({"model_name": model, "port": port,
                       "slot_save_path": slot, "tokenizer": tok}, fh)
    return d


class _Handler(BaseHTTPRequestHandler):
    code, sleep = 200, 0.0
    body = b'{"status":"ok","context":{"native":131072}}'

    def do_GET(self):
        if self.sleep:
            import time
            time.sleep(self.sleep)
        body = self.body
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass                      # il cliente ha gia' chiuso (timeout): rumore di serie

    def log_message(self, *a):
        pass


def serve(code=200, sleep=0.0, body=None):
    h = type("H", (_Handler,),
             {"code": code, "sleep": sleep, "body": body or _Handler.body})
    srv = HTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv



class RunScriptTests(unittest.TestCase):
    """run-strata-proxy.sh: venv por suporte, config criado coerente, checagem do motor."""

    def setUp(self):
        self.tree = make_tree()
        self.strata = strata_dir(self.tree, os.path.join(self.tree, "sessions"))
        self.cfg = os.path.join(self.tree, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def write_config(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    def read_config(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return json.load(fh)

    def env(self, url, extra=None):
        e = {"SKIP_SETUP": "1", "STRATA_URL": url, "STRATA_DIR": self.strata,
             "TOKENIZER": os.path.join(self.tree, "tok"), "CONFIG": "./config.json",
             "DATA": "./data-test", "PROBE_TIMEOUT": "2"}
        e.update(extra or {})
        return e

    def boot(self, code=200, sleep=0.0, extra=None, body=None):
        url, srv = serve(code, sleep, body)
        rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree, env=self.env(url, extra))
        srv.shutdown()
        srv.server_close()
        return rc, out

    def test_tokenizer_deduzido_do_config_do_motor(self):
        """TOKENIZER non e' obbligatorio: il lancatore lo legge dalla chiave "tokenizer"
        del JSON del motore, come gia' fa con slot_save_path. Nessun percorso personale
        nel lancatore: chi ha il motore in una cartella qualsiasi lo trova da solo."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_auto")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"slot_save_path": os.path.join(self.tree, "sessions"),
                       "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("tokenizer=" + tok, out)
        self.assertIn("--tokenizer " + tok, out)

    def test_sem_tokenizer_o_proxy_sobe_sem_o_argumento(self):
        """Nessun tokenizer nel config del motore: il lancatore non deve passare
        --tokenizer. Il proxy conta caratteri / chars_per_token, che e' la stima
        documentata - non un errore, e non si passa un path vuoto."""
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--tokenizer", out)
        self.assertIn("chars/3.5", out)

    def test_dois_configs_sem_identificacao_nao_escolhe_nada(self):
        """Dois packs na pasta do motor, nenhum identificador do motor em uso: o
        lancador nao passa --tokenizer e nao deduz slot_dir. Um arquivo de outro
        pack daria numeros errados e sessoes no lugar errado; a estimativa
        chars/3.5 e o sessions da pasta do Strata sao mais honestos."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        # la riga del proxy e' quella che conta: l'avviso parla di --tokenizer, la
        # chiamata no.
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertIn("chars/3.5", out)
        self.assertIn("non identificato", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_dois_configs_com_mesmo_model_name_nao_escolhe_nada(self):
        """Dois arquivos que declaram o MESMO model_name, com tokenizer e slot
        proprios: o 'model' do motor nao desempata (casa os dois) e a porta da URL
        nao casa nenhum. Nada e escolhido -- dois candidatos, um lancador honesto."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB")),
                            ("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA"))])
        with open(os.path.join(d, "strata-swift-copia.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "swift", "port": 9999,
                       "tokenizer": os.path.join(self.tree, "tokC"),
                       "slot_save_path": os.path.join(self.tree, "sessC")}, fh)
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokB", out)
        self.assertNotIn("tokC", out)
        self.assertIn("non identificato", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_model_do_motor_escolhe_tokenizer_e_slot_do_mesmo_pack(self):
        """Com dois configs, o 'model' de /v1/status decide o arquivo: tokenizer e
        slot_dir vem do MESMO pack (o do motor em uso), nao do primeiro em ordem
        alfabetica (coder vem antes de swift, e e' justamente o que nao queremos)."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_porta_da_url_escolhe_tokenizer_e_slot_do_mesmo_pack(self):
        """Sem 'model' em /v1/status, a porta da URL identifica o arquivo: tokenizer
        e slot_dir vem do MESMO pack, o que tem o port igual ao do STRATA_URL."""
        stub_venv(self.tree, ".venv")
        url, srv = serve(200)
        try:
            port = int(url.rsplit(":", 1)[1])
            d = strata_configs(self.tree, "multi",
                               [("coder", os.path.join(self.tree, "tokA"), 8081,
                                 os.path.join(self.tree, "sessA")),
                                ("swift", os.path.join(self.tree, "tokB"), port,
                                 os.path.join(self.tree, "sessB"))])
            rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree,
                          env=self.env(url, {"TOKENIZER": "", "STRATA_DIR": d}))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_arquivo_unico_da_pasta_preserva_o_comportamento(self):
        """Um unico JSON na pasta do motor: tokenizer e slot_dir daquele arquivo,
        exatamente como antes da regra de identificacao."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_uno")
        slot = os.path.join(self.tree, "sess_uno")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "uno", "port": 8080, "tokenizer": tok,
                       "slot_save_path": slot}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], slot)

    def test_sem_slot_save_path_as_sessoes_vao_para_a_pasta_do_strata(self):
        """JSON sem slot_save_path: nada e inventado. O slot cai na convencao
        $STRATA_DIR/sessions e o tokenizer continua a vir do mesmo arquivo."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_noslot")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "noslot", "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.strata, "sessions"))

    def test_tokenizer_e_slot_dir_explicitos_tem_precedencia(self):
        """TOKENIZER e SLOT_DIR dados por quem chama mandam, mesmo com dois configs
        e um motor que nao se identifica: a descoberta nao os sobrescreve."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": os.path.join(self.tree, "tok_escolhido"),
                                   "SLOT_DIR": os.path.join(self.tree, "sess_escolhida"),
                                   "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tok_escolhido"), out)
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertEqual(self.read_config()["slot_dir"],
                         os.path.join(self.tree, "sess_escolhida"))

    def test_sem_python_314_sobe_e_cria_config_sem_kv(self):
        stub_venv(self.tree, ".venv")                      # python sem compression.zstd
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        cfg = self.read_config()
        self.assertIs(cfg["kv_archive"], False, "config criado nao pode pedir kv sem suporte")
        self.assertEqual(cfg["slot_dir"], os.path.join(self.tree, "sessions"))
        self.assertIn("kv_archive=off", out)

    def test_com_suporte_cria_config_com_kv(self):
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], True)
        self.assertIn("kv_archive=on", out)

    def test_escolhe_o_venv_que_tem_suporte(self):
        stub_venv(self.tree, ".venv")
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("venv=.venv314", out)
        self.assertIn("kv_archive=on", out)

    def test_config_pessoal_com_kv_sem_suporte_falha_sem_tocar_no_config(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": True, "response_floor": 16384})
        antes = open(self.cfg, "rb").read()
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("compression.zstd", out)
        self.assertIn("nao foi tocado", out)
        self.assertEqual(open(self.cfg, "rb").read(), antes, "config pessoal nao pode ser alterado")
        self.assertEqual(self.read_config()["slot_dir"], "/meu/slot")

    def test_config_pessoal_sem_kv_sobe_sem_314(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": False})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)
        self.assertIn("--config ./config.json", out)

    def test_config_pessoal_sem_chave_kv_e_tratado_como_sem_kv(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot"})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)

    def test_json_do_config_invalido_falha(self):
        stub_venv(self.tree, ".venv")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("{ isto nao e json")
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("JSON invalido", out)

    def test_http_503_do_motor_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=503)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 503", out)

    def test_http_404_de_porta_de_outra_coisa_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=404)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 404", out)

    def test_conn_recusada_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("conexao", out)

    def test_timeout_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(sleep=3.0, extra={"PROBE_TIMEOUT": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("conexao", out)

    def test_timeout_invalido_sem_curl_nao_inventa_http(self):
        """Senza curl il probe cade sul python: un PROBE_TIMEOUT non numerico non deve
        dare un codice HTTP vuoto, che verrebbe letto come 'la porta ha risposto'."""
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": False})
        nobin = os.path.join(self.tree, "nobin")
        os.makedirs(nobin, exist_ok=True)
        os.symlink(shutil.which("dirname"), os.path.join(nobin, "dirname"))
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1",
                                   "PROBE_TIMEOUT": "abc", "PATH": nobin})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("conexao", out)
        self.assertNotIn("respondeu HTTP", out)

    def test_sem_venv_nao_sobe(self):
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)


def python_sem_pip(path):
    """python que falha em `-m pip`: e o que um venv parado no meio mostra ao setup.sh."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then exit 1; fi\n"
                 "exec " + sys.executable + " \"$@\"\n")
    os.chmod(path, 0o755)


class SetupScriptTests(unittest.TestCase):
    """setup.sh: checagem sem Python 3.14 e caminhos vigiados antes de qualquer rm -rf."""

    def setUp(self):
        self.tree = make_tree()

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def real_venv(self, name, with_bin=True):
        subprocess.run([sys.executable, "-m", "venv", name], cwd=self.tree,
                       capture_output=True, text=True, timeout=300, check=True)
        if with_bin:
            stub = os.path.join(self.tree, name, "bin", "mnemonic-proxy")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf 'stub\\n'\n")
            os.chmod(stub, 0o755)
        return os.path.join(self.tree, name)

    def setup(self, args=(), env=None):
        return run(["./setup.sh"] + list(args), cwd=self.tree, env=env or {}, timeout=600)

    def test_sintaxe_posix_e_bash(self):
        for name in ("setup.sh", "run-strata-proxy.sh"):
            for shell in ("sh", "bash"):
                if shutil.which(shell) is None:
                    continue
                rc, out = run([shell, "-n", name], cwd=self.tree)
                self.assertEqual(rc, 0, "%s -n %s: %s" % (shell, name, out))

    def test_check_passa_sem_python_314(self):
        self.real_venv(".venv")
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)

    def test_check_falha_sem_venv(self):
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("FALTA", out)

    def test_no_kv_nao_tenta_uv_e_sai_zero(self):
        self.real_venv(".venv")
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)
        self.assertNotIn("criando .venv314", out)

    def test_venv_main_fora_do_projeto_nada_e_removido(self):
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        marcador = os.path.join(fora, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intacto")
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": fora})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)
        self.assertIn("Nada foi removido", out)
        self.assertTrue(os.path.exists(marcador), "nada pode ser apagado fora do projeto")
        shutil.rmtree(fora, ignore_errors=True)

    def test_venv_main_com_dotdot_e_recusado(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "../fora"})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)

    def test_venv_main_raiz_do_projeto_e_recusada(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "."})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)

    def test_venv_kv_caminho_da_home_e_recusado(self):
        rc, out = self.setup(["--no-sudo"], {"HOME": self.tree, "VENV_KV": self.tree + "/.venv314"})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)

    def test_venv_main_symlink_e_recusado(self):
        alvo = os.path.join(self.tree, "alvo")
        os.makedirs(alvo)
        os.symlink(alvo, os.path.join(self.tree, ".venv"))
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": ".venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)

    def test_nao_remove_pasta_que_nao_e_venv(self):
        binp = os.path.join(self.tree, ".venv", "bin")
        os.makedirs(binp)
        python_sem_pip(os.path.join(binp, "python"))
        marcador = os.path.join(self.tree, ".venv", "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intacto")
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 2, out)
        self.assertIn("nao e um venv", out)
        self.assertTrue(os.path.exists(marcador), "sem pyvenv.cfg nao se remove nada")

    def test_venv_em_subpasta_inexistente_e_aceito(self):
        """Caminho legitimo dentro do projeto cujo pai ainda nao existe
        (VENV_MAIN=venvs/kv314): e o caso normal de quem escolhe onde criar o
        venv, e nao pode ser lido como 'inseguro'."""
        rc, out = self.setup(env={"VENV_MAIN": "venvs/kv314"})
        self.assertNotIn("inseguro", out)
        self.assertNotEqual(rc, 2, out)

    def test_pai_symlink_para_fora_nada_e_removido(self):
        """atalho -> pasta de fora; atalho/.venv e um venv legitimo la fora. O rm -rf
        seguiria o symlink: a validacao tem de pegar o ancestral, nao so a folha."""
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        ext = os.path.join(fora, ".venv")
        os.makedirs(os.path.join(ext, "bin"))
        with open(os.path.join(ext, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = %s\n" % os.path.dirname(sys.executable))
        python_sem_pip(os.path.join(ext, "bin", "python"))
        marcador = os.path.join(ext, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intacto")
        os.symlink(fora, os.path.join(self.tree, "atalho"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "atalho/.venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)
        self.assertIn("Nada foi removido", out)
        self.assertTrue(os.path.isdir(ext), "o venv externo nao pode ser removido")
        self.assertTrue(os.path.exists(marcador), "o conteudo externo nao pode ser tocado")
        shutil.rmtree(fora, ignore_errors=True)

    def test_ancestral_symlink_em_caminho_absoluto_tambem_e_recusado(self):
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        ext = os.path.join(fora, ".venv")
        os.makedirs(os.path.join(ext, "bin"))
        with open(os.path.join(ext, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = x\n")
        python_sem_pip(os.path.join(ext, "bin", "python"))
        marcador = os.path.join(ext, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intacto")
        os.symlink(fora, os.path.join(self.tree, "atalho"))
        rc, out = self.setup(["--no-sudo", "--no-kv"],
                             {"VENV_MAIN": os.path.join(self.tree, "atalho", ".venv")})
        self.assertEqual(rc, 2, out)
        self.assertIn("inseguro", out)
        self.assertTrue(os.path.exists(marcador), "o conteudo externo nao pode ser tocado")
        shutil.rmtree(fora, ignore_errors=True)

    def test_venv_em_subpasta_do_projeto_continua_aceito(self):
        """A validacao nao pode restringir alem do que a interface documenta: um
        VENV_MAIN legitimo em subpasta tem de passar (e o setup segue para o resto)."""
        os.makedirs(os.path.join(self.tree, "sub"))
        self.real_venv(os.path.join("sub", ".venv"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "sub/.venv"})
        self.assertNotEqual(rc, 2, out)
        self.assertNotIn("inseguro", out)
        self.assertEqual(rc, 0, out)
    def test_sem_home_definido_o_script_nao_quebra(self):
        """HOME pode faltar (cron, systemd, env -i). Com `set -u` o script nao pode
        morrer com 'parameter not set', e nao pode sair com o 2 da rejeicao de seguranca."""
        env = dict(os.environ)
        env.pop("HOME", None)
        p = subprocess.run(["./setup.sh", "--check"], cwd=self.tree, env=env,
                           capture_output=True, text=True, timeout=300)
        out = p.stdout + p.stderr
        self.assertNotIn("parameter not set", out)
        self.assertNotEqual(p.returncode, 2, out)
        self.assertIn("FALTA", out)
