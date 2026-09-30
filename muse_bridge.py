#!/usr/bin/env python3
"""
muse_bridge.py
Ponte Muse 2 -> OSC para PC, compatível com os endereços OSC do Mind Monitor.

Fluxo:
    Muse 2 --BLE--> muselsl (subprocesso) --LSL--> este script --OSC/UDP--> TouchDesigner

Uso rápido:
    python muse_bridge.py                  # procura o Muse e envia para 127.0.0.1:5000
    python muse_bridge.py --simulate       # sem Muse: dados sintéticos para testar o patch
    python muse_bridge.py --list           # lista os Muses visíveis
    python muse_bridge.py --no-stream      # usa um stream LSL já aberto (ex.: BlueMuse)
    python muse_bridge.py --help
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
import traceback

import numpy as np
from pythonosc.udp_client import SimpleUDPClient

EEG_FS = 256.0
EEG_NAMES = ("TP9", "AF7", "AF8", "TP10")
BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (7.5, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 44.0),
}
AUX_ADDR = {"ACC": "/muse/acc", "GYRO": "/muse/gyro", "PPG": "/muse/ppg"}
HS_GOOD, HS_MEDIUM, HS_BAD = 1.0, 2.0, 4.0  # mesma convenção do Mind Monitor

_running = True


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _stop(*_):
    global _running
    _running = False


# --------------------------------------------------------------------- buffer

class Ring:
    """Buffer circular (amostras x canais)."""

    def __init__(self, size: int, channels: int):
        self.size = size
        self.data = np.zeros((size, channels))
        self.count = 0

    def extend(self, x: np.ndarray) -> None:
        n = len(x)
        if n >= self.size:
            self.data[:] = x[-self.size:]
        else:
            self.data = np.roll(self.data, -n, axis=0)
            self.data[-n:] = x
        self.count = min(self.size, self.count + n)

    @property
    def full(self) -> bool:
        return self.count >= self.size


# -------------------------------------------------------------------- fontes

class LSLSource:
    """Lê um stream LSL (muselsl ou BlueMuse) por tipo: EEG, PPG, ACC, GYRO."""

    def __init__(self, stype: str, timeout: float):
        try:
            import pylsl
        except ImportError:
            sys.exit("pylsl não está instalado. Corre: pip install -r requirements.txt")
        deadline = time.perf_counter() + timeout
        found = []
        while _running and not found and time.perf_counter() < deadline:
            found = pylsl.resolve_byprop("type", stype, timeout=1.0)
        if not found:
            raise LookupError(stype)
        info = found[0]
        self.inlet = pylsl.StreamInlet(info, max_buflen=5)
        self.fs = info.nominal_srate()
        self.channels = info.channel_count()

    def pull(self):
        samples, _ = self.inlet.pull_chunk(timeout=0.0)
        if not samples:
            return None
        return np.asarray(samples, dtype=float)


class SimSource:
    """Dados sintéticos com o mesmo formato do muselsl (para testar sem Muse)."""

    FS = {"EEG": 256.0, "ACC": 52.0, "GYRO": 52.0, "PPG": 64.0}
    CH = {"EEG": 5, "ACC": 3, "GYRO": 3, "PPG": 3}

    def __init__(self, kind: str, rng: np.random.Generator):
        self.kind, self.rng = kind, rng
        self.fs, self.channels = self.FS[kind], self.CH[kind]
        self.t0 = time.perf_counter()
        self.n = 0

    def pull(self):
        target = int((time.perf_counter() - self.t0) * self.fs)
        k = target - self.n
        if k <= 0:
            return None
        t = (self.n + np.arange(k)) / self.fs
        self.n = target
        r = self.rng
        if self.kind == "EEG":
            alpha_amp = 12 + 10 * np.sin(2 * np.pi * 0.05 * t)  # alpha sobe/desce em 20 s
            common = (alpha_amp * np.sin(2 * np.pi * 10 * t)
                      + 6 * np.sin(2 * np.pi * 6 * t + 1.0)
                      + 3 * np.sin(2 * np.pi * 20 * t))
            out = np.column_stack([common + r.normal(0, 8, k) for _ in range(5)])
            blink = 250 * np.exp(-((t % 5.0) - 2.5) ** 2 / (2 * 0.05 ** 2))  # piscar a cada 5 s
            out[:, 1] += blink
            out[:, 2] += blink
            return out
        if self.kind == "PPG":
            v = 1000 + 200 * np.sin(2 * np.pi * 1.2 * t)
            return np.column_stack([v + r.normal(0, 5, k) for _ in range(3)])
        if self.kind == "ACC":
            return np.column_stack([r.normal(0, 0.01, k), r.normal(0, 0.01, k),
                                    1 + r.normal(0, 0.01, k)])
        return r.normal(0, 0.5, (k, 3))


# ------------------------------------------------------------------- análise

class Analyzer:
    def __init__(self, fs: float, win: int):
        self.fs, self.win = fs, win
        self.hann = np.hanning(win)
        self.norm = fs * np.sum(self.hann ** 2)
        freqs = np.fft.rfftfreq(win, 1 / fs)
        self.df = fs / win
        self.masks = {b: (freqs >= lo) & (freqs < hi) for b, (lo, hi) in BANDS.items()}
        # atividade muscular (maxilar): acima da gama EEG, a evitar 50 e 100 Hz da rede
        self.emg_mask = (freqs >= 55) & (freqs < 95)

    def psd(self, x: np.ndarray) -> np.ndarray:
        x = x - x.mean(axis=0)
        spec = np.fft.rfft(x * self.hann[:, None], axis=0)
        p = np.abs(spec) ** 2 / self.norm
        p[1:-1] *= 2
        return p

    def bands(self, p: np.ndarray):
        power = {b: np.maximum(p[m].sum(axis=0) * self.df, 1e-12) for b, m in self.masks.items()}
        total = sum(power.values())
        absolute = {b: np.log10(v) for b, v in power.items()}  # log10 (Bels), como o Mind Monitor
        relative = {b: v / total for b, v in power.items()}    # 0..1, soma = 1 por canal
        return absolute, relative

    def emg(self, p: np.ndarray) -> np.ndarray:
        return np.log10(np.maximum(p[self.emg_mask].sum(axis=0) * self.df, 1e-12))


def contact_quality(x: np.ndarray, good: float, medium: float):
    """Horseshoe por canal a partir do desvio robusto (MAD), pouco sensível a piscares."""
    x = x - np.median(x, axis=0)
    sd = 1.4826 * np.median(np.abs(x), axis=0)
    hs = np.where(sd < good, HS_GOOD, np.where(sd < medium, HS_MEDIUM, HS_BAD))
    hs[sd < 0.5] = HS_BAD  # sinal plano = sem contacto
    return hs, sd


# ------------------------------------------------------------------- sessão

def send_elements(client, args, absolute, relative, hs, touching):
    def vals(v):
        return [float(np.mean(v))] if args.average else [float(a) for a in v]

    if args.bands in ("absolute", "both"):
        for b, v in absolute.items():
            client.send_message(f"/muse/elements/{b}_absolute", vals(v))
    if args.bands in ("relative", "both"):
        for b, v in relative.items():
            client.send_message(f"/muse/elements/{b}_relative", vals(v))
    client.send_message("/muse/elements/horseshoe", [float(h) for h in hs])
    client.send_message("/muse/elements/touching_forehead", int(touching))


def connect_sources(args):
    kinds = ["EEG", "ACC", "GYRO", "PPG"]
    if args.simulate:
        rng = np.random.default_rng()
        return {k: SimSource(k, rng) for k in kinds}
    src = {"EEG": LSLSource("EEG", args.timeout)}
    for k in kinds[1:]:
        try:
            src[k] = LSLSource(k, 2.0)
        except LookupError:
            log(f"Stream {k} não encontrado (continua sem ele).")
    return src


def run_session(src, proc, client, args):
    eeg_src = src["EEG"]
    fs = eeg_src.fs or EEG_FS
    win = int(args.window * fs)
    ring = Ring(win, 4)
    an = Analyzer(fs, win)
    period = 1.0 / args.rate
    blink_n = int(0.3 * fs)

    now = time.perf_counter()
    next_t = last_data = now
    last_blink = last_jaw = last_debug = 0.0

    client.send_message("/bridge/connected", 1)
    log(f"Ligado. {eeg_src.channels} canais EEG a {fs:.0f} Hz -> OSC {args.ip}:{args.port}")

    while _running:
        now = time.perf_counter()

        eeg = eeg_src.pull()
        if eeg is not None:
            last_data = now
            if not args.no_raw:
                for s in eeg:
                    client.send_message("/muse/eeg", [float(v) for v in s])
            ring.extend(eeg[:, :4])

        for k, addr in AUX_ADDR.items():
            if k in src:
                d = src[k].pull()  # puxa sempre, para não acumular no inlet
                if d is not None and not args.no_raw:
                    for s in d:
                        client.send_message(addr, [float(v) for v in s])

        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"muselsl terminou (código {proc.returncode}).")
        if now - last_data > args.stall:
            raise RuntimeError(f"Sem dados EEG há {args.stall:.0f} s.")

        if ring.full and now >= next_t:
            next_t = now + period
            x = ring.data
            p = an.psd(x)
            absolute, relative = an.bands(p)
            hs, sd = contact_quality(x, args.good, args.medium)
            touching = int(np.sum(hs < HS_BAD) >= 2)
            send_elements(client, args, absolute, relative, hs, touching)

            blink_peak = jaw = float("nan")
            if not args.no_events and touching:
                front = x[:, 1:3]
                recent = front[-blink_n:] - np.median(front, axis=0)
                blink_peak = float(recent.mean(axis=1).max())
                if blink_peak > args.blink_thresh and now - last_blink > 0.5:
                    client.send_message("/muse/elements/blink", 1)
                    last_blink = now
                emg = an.emg(p)
                jaw = float(np.mean(emg[[0, 3]]))  # TP9 + TP10
                if jaw > args.jaw_thresh and now - last_jaw > 1.0:
                    client.send_message("/muse/elements/jaw_clench", 1)
                    last_jaw = now

            if args.debug and now - last_debug >= 1.0:
                last_debug = now
                q = " ".join(f"{n}:{s:5.1f}" for n, s in zip(EEG_NAMES, sd))
                log(f"alpha abs {np.mean(absolute['alpha']):5.2f} | "
                    f"alpha rel {np.mean(relative['alpha']):4.2f} | "
                    f"sd {q} | blink {blink_peak:6.1f} | jaw {jaw:5.2f}")

        time.sleep(0.004)


def start_muselsl(args):
    cmd = [sys.executable, "-m", "muselsl", "stream", "--ppg", "--acc", "--gyro"]
    if args.address:
        cmd += ["--address", args.address]
    if args.backend:
        cmd += ["--backend", args.backend]
    log("A arrancar: muselsl " + " ".join(cmd[3:]))
    return subprocess.Popen(cmd)


def stop_proc(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------- main

def parse_args():
    ap = argparse.ArgumentParser(description="Muse 2 -> OSC (compatível com Mind Monitor)")
    g = ap.add_argument_group("ligação")
    g.add_argument("--ip", default="127.0.0.1", help="destino OSC (default 127.0.0.1)")
    g.add_argument("--port", type=int, default=5000, help="porta OSC (default 5000, igual ao Mind Monitor)")
    g.add_argument("--address", help="MAC/ID do Muse (se houver mais do que um)")
    g.add_argument("--backend", choices=["auto", "bleak", "bgapi", "bluemuse", "gatt"],
                   help="backend BLE do muselsl (bgapi = dongle BLED112)")
    g.add_argument("--no-stream", action="store_true",
                   help="não arranca o muselsl; usa um stream LSL já aberto (ex.: BlueMuse)")
    g.add_argument("--simulate", action="store_true", help="dados sintéticos, sem Muse")
    g.add_argument("--list", action="store_true", help="lista Muses visíveis e sai")

    g = ap.add_argument_group("saída")
    g.add_argument("--bands", choices=["absolute", "relative", "both"], default="both")
    g.add_argument("--average", action="store_true",
                   help="um valor por banda (média dos 4 sensores) em vez de 4")
    g.add_argument("--rate", type=float, default=10.0, help="Hz das bandas/horseshoe (default 10)")
    g.add_argument("--window", type=float, default=1.0, help="janela da FFT em s (default 1.0)")
    g.add_argument("--no-raw", action="store_true", help="não envia /muse/eeg, acc, gyro, ppg")

    g = ap.add_argument_group("deteção (calibrar com --debug)")
    g.add_argument("--good", type=float, default=50.0, help="desvio µV abaixo do qual o contacto é bom")
    g.add_argument("--medium", type=float, default=100.0, help="desvio µV abaixo do qual é médio")
    g.add_argument("--blink-thresh", type=float, default=120.0, help="pico µV frontal para piscar")
    g.add_argument("--jaw-thresh", type=float, default=2.5, help="log10 da potência 55-95 Hz para maxilar")
    g.add_argument("--no-events", action="store_true", help="desliga blink e jaw_clench")

    g = ap.add_argument_group("robustez")
    g.add_argument("--timeout", type=float, default=30.0, help="s à espera do stream EEG")
    g.add_argument("--stall", type=float, default=5.0, help="s sem dados até reconectar")
    g.add_argument("--retry", type=float, default=3.0, help="s entre tentativas")
    g.add_argument("--debug", action="store_true", help="imprime valores 1x/s para calibrar")
    return ap.parse_args()


def main():
    args = parse_args()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.list:
        cmd = [sys.executable, "-m", "muselsl", "list"]
        if args.backend:
            cmd += ["--backend", args.backend]
        sys.exit(subprocess.call(cmd))

    client = SimpleUDPClient(args.ip, args.port)
    log(f"Muse bridge -> OSC {args.ip}:{args.port}" + ("  [SIMULAÇÃO]" if args.simulate else ""))

    while _running:
        proc = None
        try:
            if not (args.simulate or args.no_stream):
                proc = start_muselsl(args)
            log("À procura do stream EEG...")
            src = connect_sources(args)
            run_session(src, proc, client, args)
        except LookupError:
            log("Stream EEG não encontrado. O Muse está ligado e perto?")
        except RuntimeError as e:
            log(str(e))
        except Exception:
            log("Erro inesperado:\n" + traceback.format_exc())
        finally:
            stop_proc(proc)
            try:
                client.send_message("/bridge/connected", 0)
            except OSError:
                pass

        if _running:
            log(f"Nova tentativa em {args.retry:.0f} s...")
            end = time.perf_counter() + args.retry
            while _running and time.perf_counter() < end:
                time.sleep(0.1)

    log("Terminado.")


if __name__ == "__main__":
    main()
