"""Salvataggio / ripristino automatico del segmento attivo.

Strata tiene UNA conversazione (più pochi checkpoint) e la perde al riavvio o quando un'altra conversazione la
sostituisce. Rileggere 70–100K token costa 30–40 s; ripristinare un file di sessione ~1–2 s.

- `Tracker` avvolge l'Upstream e sa che cosa Strata ha in memoria: dopo ogni chiamata registra l'impronta del prompt
  (testo del template SENZA il prompt di generazione: è esattamente il checkpoint di fine turno che Strata riusa) e
  il contatore `requests` di /v1/status. Prima di ogni chiamata: se Strata è ripartito (started diverso, o non
  caricato), se qualcun altro l'ha usato (requests diverso) o se tiene un'altra conversazione, e c'è un autosalvataggio
  il cui testo è prefisso ESATTO del nuovo prompt e vale almeno `autorestore_min_gain` token in più, fa `restore`.
- `AutoSaver` (thread): quando la conversazione che occupa Strata è ferma da `autosave_idle_s`, nessuna richiesta è
  in corso (lock del proxy libero, /v1/status senza in_flight) e lo stato è ancora il nostro, `save` in un file
  `<conv>-seg<N>-auto-<impronta>.bin`; registra file/token/byte/impronta nella tabella `autosaves`. Pulizia: un file
  il cui testo è prefisso esatto di quello appena salvato è superato e si cancella; poi al massimo `autosave_keep`
  file e `autosave_max_gb`. Testo e archivio SQLite restano sempre.
- Lo stato del Tracker è salvato nella tabella `kv`: dopo un riavvio del PROXY con Strata ancora vivo e intatto
  (stesso started/requests) non serve alcun restore.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

from .core import H
from .render import render, template_kwargs
from .upstream import UpstreamError

STATE_KEY = "strata_state"


def prompt_text(body: dict) -> str | None:
    """Testo del prompt fino all'ultimo confine di turno (dove Strata mette il checkpoint riusabile)."""
    try:
        return render(body.get("messages") or [], body.get("tools") or None, template_kwargs(body),
                      add_generation_prompt=False)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class _TrackedStream:
    def __init__(self, call, on_done, on_abort):
        self.call, self.on_done, self.on_abort = call, on_done, on_abort
        self.finished = False

    def __iter__(self):
        usage = None
        for ch in self.call:
            if ch.get("usage"):
                usage = ch["usage"]
            yield ch
        self.finished = True
        self.on_done(usage)

    def close(self):
        self.call.close()
        if not self.finished:
            self.finished = True
            self.on_abort()


class Tracker:
    """Upstream con memoria di ciò che Strata contiene. Stessa interfaccia di Upstream (chat, chat_stream, slot, raw)."""

    def __init__(self, up, store, journal, cfg):
        self.up, self.store, self.journal, self.cfg = up, store, journal, cfg
        self.ctx: tuple[str | None, int] = (None, 0)       # (conv, seg) della richiesta in corso (lo imposta il proxy)
        self.saved: dict[str, dict] = {}                     # file salvati da questo processo -> stato
        self.skip_restore = False
        self.text = None                                     # testo dell'ultimo prompt letto da Strata (questo processo)
        self.mu = threading.RLock()
        self.state = self._load()

    # ---------- passthrough ----------
    def __getattr__(self, k):
        return getattr(self.up, k)

    def raw(self, *a, **kw):
        return self.up.raw(*a, **kw)

    # ---------- stato persistente ----------
    def _load(self):
        try:
            v = self.store.kv_get(STATE_KEY)
            return json.loads(v) if v else None
        except (ValueError, TypeError):
            return None

    def _set(self, st):
        with self.mu:
            self.state = st
            try:
                self.store.kv_set(STATE_KEY, json.dumps(st) if st else "")
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict | None:
        try:
            st, _, data = self.up.raw("GET", "/v1/status")
            if st != 200:
                return None
            return json.loads(data)
        except Exception:  # noqa: BLE001
            return None

    def check(self, s: dict | None = None, where: str = "") -> dict | None:
        """Lo stato registrato vale ancora? Altrimenti lo azzera (e lo annota). -> stato valido o None."""
        st = self.state
        if st is None:
            return None
        s = s if s is not None else self.status()
        why = None
        if s is None:
            why = "status_unavailable"
        elif not s.get("loaded", True):
            why = "unloaded"
        elif st.get("started") is not None and s.get("started") != st.get("started"):
            why = "strata_restart"
        elif st.get("requests") is not None and \
                (s.get("activity") or {}).get("requests") not in (st["requests"], st["requests"] - 1):
            # -1: il totale di Strata può aggiornarsi un attimo dopo la fine della risposta
            why = "foreign_request"
        if why:
            self.journal.log("strata_state_lost", reason=why, conv=st.get("conv"), seg=st.get("seg"),
                             tokens=st.get("tokens"), where=where)
            self._set(None)
            return None
        return st

    # ---------- chiamate ----------
    def _before(self, body: dict):
        """-> (testo del prompt, stato /v1/status prima della chiamata). Eventuale restore automatico."""
        text = prompt_text(body)
        s = self.status()
        st = self.check(s, "before_call")
        if self.skip_restore:
            self.skip_restore = False
        elif text is not None and self.cfg.autorestore:
            self._maybe_restore(text, st, s)
            s = self.status()
        return text, s

    def _after(self, text, s, usage, ok=True):
        if not ok or text is None or s is None:
            self._set(None)
            return
        conv, seg = self.ctx
        self.text = text
        self._set({"phash": H(text), "plen": len(text), "tokens": int((usage or {}).get("prompt_tokens") or 0),
                   "conv": conv, "seg": seg, "started": s.get("started"),
                   "requests": ((s.get("activity") or {}).get("requests") or 0) + 1, "t_end": time.time()})

    def chat(self, body: dict) -> dict:
        text, s = self._before(body)
        try:
            r = self.up.chat(body)
        except Exception:
            self._set(None)
            raise
        self._after(text, s, r.get("usage"))
        return r

    def chat_stream(self, body: dict):
        text, s = self._before(body)
        try:
            call = self.up.chat_stream(body)
        except Exception:
            self._set(None)
            raise
        return _TrackedStream(call, lambda u: self._after(text, s, u), lambda: self._set(None))

    def slot(self, action: str, filename: str) -> dict:
        r = self.up.slot(action, filename)
        if action == "save" and self.state:
            self.saved[filename] = dict(self.state)
        elif action == "restore":
            known = self.saved.get(filename) or self.store.autosave_state(filename)
            s = self.status()
            if known and s:
                self._set({**known, "started": s.get("started"),
                           "requests": (s.get("activity") or {}).get("requests"), "t_end": time.time()})
            else:
                self._set(None)
                self.skip_restore = True     # restore deciso dal Manager (ancora): non sovrascriverlo
        return r

    # ---------- ripristino automatico ----------
    def _maybe_restore(self, text: str, st: dict | None, s: dict | None):
        if s is None:
            return
        have = 0
        if st and st["plen"] <= len(text) and H(text[:st["plen"]]) == st["phash"]:
            have = st["tokens"]
        best = None
        for row in self.store.autosaves(active_only=True):
            if row["plen"] <= len(text) and (best is None or row["tokens"] > best["tokens"]) \
                    and H(text[:row["plen"]]) == row["phash"]:
                best = row
        if best is None or best["tokens"] - have < self.cfg.autorestore_min_gain:
            return
        t0 = time.time()
        reason = "strata_restart" if st is None else "other_state"
        if not s.get("loaded", True):
            # motore spento (scaricato o morto): il restore vuole il modello caricato, lo si carica ora
            try:
                self.up.raw("POST", "/load", b"{}")
            except Exception:  # noqa: BLE001
                pass
        try:
            r = self.up.slot("restore", best["file"])
        except UpstreamError as e:
            self.journal.log("autorestore_error", conv=best["conv"], file=best["file"], status=e.status,
                             error=str(e)[:300])
            if e.status == 404:
                self.store.autosave_deleted(best["file"])
            self._set(None)
            return
        ms = round((time.time() - t0) * 1000)
        self.store.autosave_used(best["file"])
        s2 = self.status() or {}
        self._set({**{k: best[k] for k in ("phash", "plen", "tokens", "conv", "seg")},
                   "started": s2.get("started"), "requests": (s2.get("activity") or {}).get("requests"),
                   "t_end": time.time()})
        self.journal.log("autorestore", conv=best["conv"], seg=best["seg"], file=best["file"], tokens=best["tokens"],
                         had_tokens=have, gain_tokens=best["tokens"] - have, reason=reason, ms=ms,
                         restore_ms=(r.get("timings") or {}).get("restore_ms"), n_restored=r.get("n_restored"))


class AutoSaver:
    def __init__(self, proxy):
        self.proxy = proxy
        self.cfg, self.store, self.journal = proxy.cfg, proxy.store, proxy.journal
        self.tr: Tracker = proxy.up
        self.stop_ev = threading.Event()
        self.th = None
        self._skip_logged = None

    def start(self):
        self.th = threading.Thread(target=self._loop, daemon=True, name="autosave")
        self.th.start()
        return self

    def stop(self):
        self.stop_ev.set()

    def _loop(self):
        while not self.stop_ev.wait(self.cfg.autosave_poll_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.journal.log("autosave_error", step="tick", error=repr(e)[:300])

    def _skip(self, reason, **kw):
        key = (reason, (self.tr.state or {}).get("phash"))
        if key != self._skip_logged:
            self._skip_logged = key
            self.journal.log("autosave_skip", reason=reason, **kw)
        return None

    def tick(self, now: float | None = None):
        """Un controllo; -> evento autosave o None."""
        cfg, tr = self.cfg, self.tr
        now = now or time.time()
        st = tr.state
        if not st or not st.get("conv"):
            return None
        if now - st.get("t_end", now) < cfg.autosave_idle_s:
            return None
        if st.get("tokens", 0) < cfg.autosave_min_tokens:
            return None
        if self.store.autosave_with_phash(st["phash"]):
            return None                                     # già salvato
        if not self.proxy.lock.acquire(blocking=False):
            return None                                     # una richiesta è in corso: mai salvare adesso
        try:
            s = tr.status()
            if s is None or (s.get("activity") or {}).get("in_flight"):
                return None
            st = tr.check(s, "autosave")
            if st is None:
                return None
            if cfg.slot_dir:
                free = shutil.disk_usage(cfg.slot_dir).free
                need = cfg.autosave_min_free_gb * 2 ** 30 + st["tokens"] * 20000
                if free < need:
                    return self._skip("disk_low", free_gb=round(free / 2 ** 30, 1))
            fn = "%s-seg%d-auto-%s.bin" % (st["conv"], st.get("seg") or 0, st["phash"][:10])
            t0 = time.time()
            try:
                r = tr.up.slot("save", fn)
            except Exception as e:  # noqa: BLE001
                self.journal.log("autosave_error", conv=st["conv"], file=fn, error=str(e)[:300])
                return None
            ms = round((time.time() - t0) * 1000)
            nbytes = int(r.get("n_written") or 0)
            self.store.add_autosave(fn, st["conv"], st.get("seg") or 0, st["phash"], st["plen"], st["tokens"],
                                    nbytes, int(r.get("n_saved") or 0))
            ev = self.journal.log("autosave", conv=st["conv"], seg=st.get("seg") or 0, file=fn, tokens=st["tokens"],
                                  n_saved=r.get("n_saved"), bytes=nbytes, ms=ms, idle_s=round(now - st["t_end"]),
                                  phash=st["phash"][:16])
            # lo stato di Strata non cambia con un save (requests non conta i save): resta valido
            self.prune(keep_file=fn, text=getattr(tr, "text", None) if H(getattr(tr, "text", "") or "") == st["phash"]
                       else None)
            return ev
        finally:
            self.proxy.lock.release()

    def prune(self, keep_file: str | None = None, text: str | None = None):
        """Superato = un salvataggio più vecchio il cui testo è prefisso esatto di `text` (quello appena salvato):
        la stessa conversazione più avanti lo rende inutile. Poi tetto di numero e di disco (più recenti tenuti)."""
        cfg = self.cfg
        rows = self.store.autosaves(active_only=True)        # più recenti prima
        drop, kept, total = [], [], 0
        for r in rows:
            if r["file"] != keep_file and text is not None and r["plen"] <= len(text) \
                    and H(text[:r["plen"]]) == r["phash"]:
                drop.append((r, "superseded"))
                continue
            if r["file"] != keep_file and (len(kept) >= cfg.autosave_keep or
                                           total + r["bytes"] > cfg.autosave_max_gb * 2 ** 30):
                drop.append((r, "limit"))
                continue
            kept.append(r)
            total += r["bytes"]
        for r, why in drop:
            ok = True
            if cfg.slot_dir:
                try:
                    os.remove(os.path.join(cfg.slot_dir, r["file"]))
                except FileNotFoundError:
                    pass
                except OSError as e:
                    ok = False
                    self.journal.log("autosave_error", step="prune", file=r["file"], error=str(e)[:200])
            if ok:
                if cfg.kv_archive:
                    from .kvarchive import discard
                    discard(cfg, r["file"], self.journal)
                self.store.autosave_deleted(r["file"])
                self.journal.log("autosave_prune", conv=r["conv"], seg=r["seg"], file=r["file"], reason=why,
                                 bytes=r["bytes"])
        return drop
