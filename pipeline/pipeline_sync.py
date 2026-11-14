# -*- coding: utf-8 -*-
"""
pipeline_sync.py — Pipeline sinkronisasi lirik NadaKu (VPS)
Audio + Lirik -> Demucs (isolasi vokal) -> Whisper large-v3 -> Forced Alignment
-> Synchronization Correction -> Export LRC/JSON.

Per-track idempotent & resume-able: hasil disimpan ke out/<id>.json segera.
Mode:
  offset   : drift seragam -> export offset (detik)
  retimed  : drift non-seragam -> export LRC baru per-baris
  ok       : sudah sinkron (|offset| < ambang)
"""
import json, os, re, subprocess, sys
import numpy as np
import requests

GH_PREFIX = "https://github.com/kairorawr86-beep/nadaku-audio/releases/download/"
# SSRF guard: gh wajib path relatif release asset (.mp3, tanpa ../), tid alfanumerik-dash.
GH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]*\.mp3$')
TID_RE = re.compile(r'^[A-Za-z0-9_-]+$')
FINAL_URL_RE = re.compile(r'^https://([a-z0-9.-]*\.)?githubusercontent\.com/|^https://github\.com/')
WORK = os.environ.get("NADAKU_WORK", "/root/nadaku_sync")
CAP_SECONDS = int(os.environ.get("CAP_SECONDS", "150"))
NADAKU_DEVICE = os.environ.get("NADAKU_DEVICE", "cpu")
NADAKU_COMPUTE = os.environ.get("NADAKU_COMPUTE", "int8" if NADAKU_DEVICE == "cpu" else "float16")
OUT = os.path.join(WORK, "out")
AUDIO = os.path.join(WORK, "audio")
VOCALS = os.path.join(WORK, "vocals")
os.makedirs(OUT, exist_ok=True); os.makedirs(AUDIO, exist_ok=True); os.makedirs(VOCALS, exist_ok=True)

def crop_audio(mp3, tid):
    """Potong ke CAP_SECONDS detik pertama (hemat Demucs+Whisper; offset & drift terdeteksi di sini)."""
    wav = os.path.join(AUDIO, tid + f".c{CAP_SECONDS}.wav")
    if os.path.exists(wav) and os.path.getsize(wav) > 10000: return wav
    subprocess.run(["ffmpeg", "-y", "-i", mp3, "-t", str(CAP_SECONDS), "-ar", "16000", "-ac", "1", wav],
                   capture_output=True, timeout=300)
    return wav if os.path.exists(wav) and os.path.getsize(wav) > 10000 else mp3

def validate_track(track):
    if not TID_RE.match(track.get("id", "")):
        raise ValueError("id track tidak valid: " + repr(track.get("id"))[:60])
    if not GH_RE.match(track.get("gh", "")) or ".." in track["gh"]:
        raise ValueError("gh tidak valid: " + repr(track.get("gh"))[:60])

def norm(s):
    s = str(s or "").lower()
    s = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', s)
    s = re.sub(r'\b(feat|ft|featuring)\.?\s+.*$', '', s, flags=re.I)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return s.strip()

def parse_cues(lrc):
    out = []
    for line in str(lrc or "").split("\n"):
        m = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)', line)
        if m:
            x = m.group(3).strip()
            if x and not x.startswith(("♪", "○", "●")):
                out.append({"t": int(m.group(1)) * 60 + float(m.group(2)), "text": x})
    out.sort(key=lambda c: c["t"])
    return out

def download(gh, tid):
    validate_track({"id": tid, "gh": gh})
    out = os.path.join(AUDIO, tid + ".mp3")
    if os.path.exists(out) and os.path.getsize(out) > 100000: return out
    r = requests.get(GH_PREFIX + gh, timeout=300, allow_redirects=True)
    # SSRF guard: host final setelah redirect wajib masih di GitHub
    if r.url and not FINAL_URL_RE.match(r.url):
        return None
    if r.status_code not in (200, 206) or len(r.content) < 100000: return None
    open(out, "wb").write(r.content)
    return out

def demucs_vocals(mp3, tid):
    """Isolasi vokal dengan Demucs 2-stem. Output: vocals wav (mono 16k)."""
    out = os.path.join(VOCALS, tid + "_vocals.wav")
    if os.path.exists(out) and os.path.getsize(out) > 10000: return out
    tmp = os.path.join(VOCALS, "demucs_out_" + tid)
    cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs",
           "-o", tmp, "--device", NADAKU_DEVICE, mp3]
    pr = subprocess.run(cmd, capture_output=True, timeout=3600)
    # demucs output: tmp/htdemucs/<basename>/vocals.wav
    base = os.path.splitext(os.path.basename(mp3))[0]
    cand = os.path.join(tmp, "htdemucs", base, "vocals.wav")
    if not os.path.exists(cand):
        errtail = (pr.stderr or b"").decode(errors="replace").strip().splitlines()
        print("DEMUCS FAIL rc=" + str(pr.returncode) + " | " + " / ".join(errtail[-4:]), flush=True)
        return None
    conv = ["ffmpeg", "-y", "-i", cand, "-ar", "16000", "-ac", "1", out]
    subprocess.run(conv, capture_output=True, timeout=300)
    subprocess.run(["rm", "-rf", tmp], capture_output=True)
    return out if os.path.exists(out) and os.path.getsize(out) > 10000 else None

def whisper_transcribe(audio, model, language=None):
    kw = dict(word_timestamps=True, beam_size=1, condition_on_previous_text=False)
    if language: kw["language"] = language
    segments, info = model.transcribe(audio, **kw)
    segs = []
    for s in segments:
        words = []
        if s.words:
            words = [{"t": round(w.start, 2), "w": w.word.strip()} for w in s.words if w.word and w.word.strip()]
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(), "words": words})
    return segs, info

def wnorm(s): return norm(s).split(" ")

def tok_sim(a, b):
    A, B = set(wnorm(a)), set(wnorm(b))
    if not A or not B: return 0.0
    n = len(A & B)
    return n / (len(A) + len(B) - n)

def align_lines(cues, segs):
    """Forced alignment sederhana berbasis urutan: DP monotonic cue<->segmen,
    skor = kemiripan token; lalu perbaiki start cue dengan word timestamps."""
    n, m = len(cues), len(segs)
    if n == 0 or m == 0: return []
    NEG = -1e9
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    bt = [[None] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1): dp[0][j] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sc = tok_sim(cues[i - 1]["text"], segs[j - 1]["text"])
            best, arg = dp[i - 1][j - 1] + sc, "m"
            if dp[i][j - 1] > best: best, arg = dp[i][j - 1], "skip"
            if dp[i - 1][j] > best: best, arg = dp[i - 1][j], "gap"
            dp[i][j], bt[i][j] = best, arg
    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        a = bt[i][j]
        if a == "m":
            pairs.append((i - 1, j - 1, tok_sim(cues[i - 1]["text"], segs[j - 1]["text"])))
            i -= 1; j -= 1
        elif a == "gap": i -= 1
        else: j -= 1
    pairs.reverse()
    return pairs

def refine_starts(cues, segs, pairs):
    """Perbaiki start cue: cari word-window pertama yang cocok di segmen terpasang."""
    refined = {}
    for ci, si, sc in pairs:
        cue_words = wnorm(cues[ci]["text"])
        seg = segs[si]
        wds = seg["words"]
        if wds and cue_words:
            # sliding window terbaik
            L = min(len(cue_words), len(wds))
            best_s, best_sc = seg["start"], -1
            for a in range(0, max(1, len(wds) - L + 1)):
                win = " ".join(w["w"] for w in wds[a:a + L])
                sc2 = tok_sim(" ".join(cue_words[:L]), win)
                if sc2 > best_sc: best_sc, best_s = sc2, wds[a]["t"]
            refined[ci] = best_s
        else:
            refined[ci] = seg["start"]
    return refined

def build_offset_map(all_cues, refined, good):
    """Anchor (studio_t -> live_t) hanya dari pasangan berkualitas, urut audio."""
    anchors = []
    for ci, _, _ in good:
        t, a = all_cues[ci]["t"], refined[ci]
        if anchors and (a <= anchors[-1][1] + 0.05 or t <= anchors[-1][0]): continue
        anchors.append((t, a))
    return anchors

def prune_anchors(anchors):
    """Buang anchor yang merusak konsistensi slope lokal (iteratif, seperti retime)."""
    working = [list(a) for a in anchors]
    for _ in range(4):
        if len(working) < 4: break
        slopes = []
        for i in range(1, len(working)):
            dt = max(0.1, working[i][0] - working[i - 1][0])
            slopes.append((working[i][1] - working[i - 1][1]) / dt)
        med = sorted(slopes)[len(slopes) // 2]
        if med <= 0.05: break
        import math
        worst_i, worst_d = None, 0.0
        for i, s in enumerate(slopes):
            d = abs(math.log(max(s, 1e-6) / med))
            if d > worst_d: worst_i, worst_d = i + 1, d
        if worst_d < math.log(1.9): break
        del working[worst_i]
    return [tuple(a) for a in working]

def analyze_drift(anchors):
    """Deviasi p85 dari garis lurus pertama-akhir (robust terhadap 1-2 anchor salah tempel)."""
    if len(anchors) < 6: return 0.0  # bukti terlalu sedikit untuk menilai drift
    (t0, a0), (t1, a1) = anchors[0], anchors[-1]
    if t1 - t0 < 1: return 0.0
    devs = sorted(abs((a0 + (a1 - a0) * (t - t0) / (t1 - t0)) - a) for t, a in anchors)
    k = max(1, int(round(len(devs) * 0.85)))
    return devs[k - 1]

def retime_lrc(cues, anchors):
    def mapt(t):
        A = anchors
        if t <= A[0][0]: return max(0.0, A[0][1] - (A[0][0] - t) * 0.95)
        if t >= A[-1][0]: return A[-1][1] + (t - A[-1][0]) * 0.95
        for i in range(1, len(A)):
            if t <= A[i][0]:
                (ta, aa), (tb, ab) = A[i - 1], A[i]
                return aa + ((t - ta) / (tb - ta)) * (ab - aa)
        return t
    out, last = [], -1
    for c in cues:
        nt = max(0.0, round(mapt(c["t"]) * 100) / 100)
        if nt <= last: nt = last + 0.05
        last = nt
        mm = str(int(nt // 60)).zfill(2); ss = f"{nt % 60:05.2f}"
        out.append(f"[{mm}:{ss}] {c['text']}")
    return "\n".join(out), last

def process(track, model):
    tid, gh, lrc = track["id"], track["gh"], track["lrc"]
    cues = parse_cues(lrc)
    if len(cues) < 3: return {"id": tid, "mode": "skip", "reason": "lrc kurang"}
    mp3 = download(gh, tid)
    if not mp3: return {"id": tid, "mode": "skip", "reason": "download gagal"}
    mp3 = crop_audio(mp3, tid)

    # FAST PATH: Whisper langsung di slice (tanpa Demucs) — mayoritas track bersih.
    path = "fast"
    attempts = [(mp3, None), (mp3, "id")]
    segs = info = None
    good = []
    for src_audio, lang in attempts:
        segs, info = whisper_transcribe(src_audio, model, language=lang)
        pairs = align_lines(cues, segs)
        good = [(ci, si, sc) for ci, si, sc in pairs if sc >= 0.35]
        if len(good) >= 3: break

    # FALLBACK Demucs (vokal terisolasi) — untuk track sulit: MV/Live, vokal tersembunyi
    if len(good) < 3 and os.environ.get("PIPELINE_NO_DEMUCS") != "1":
        path = "demucs"
        voc = demucs_vocals(mp3, tid)
        if voc:
            for src_audio, lang in [(voc, None), (voc, "id"), (mp3, None)]:
                segs, info = whisper_transcribe(src_audio, model, language=lang)
                pairs = align_lines(cues, segs)
                good = [(ci, si, sc) for ci, si, sc in pairs if sc >= 0.35]
                if len(good) >= 3: break
    if len(good) < 3:
        return {"id": tid, "mode": "skip", "reason": f"anchor kurang ({len(good)})",
                "n_segs": len(segs), "lang": info.language, "path": path}
    refined = refine_starts(cues, segs, good)
    anchors = prune_anchors(build_offset_map(cues, refined, good))
    if len(anchors) < 3:
        return {"id": tid, "mode": "skip", "reason": "anchor monotonic kurang"}
    offs = [a - t for t, a in anchors]
    med = float(np.median(offs))
    drift = analyze_drift(anchors)
    audio_dur = segs[-1]["end"] if segs else 0
    base = {"id": tid, "artist": track.get("artist"), "title": track.get("title"),
            "lang": info.language, "n_anchor": len(anchors), "median_offset": round(med, 2),
            "drift_max": round(drift, 2), "audio_dur": round(audio_dur, 1), "path": path}
    if abs(med) < 1.25 and drift < 3.0:
        return {**base, "mode": "ok"}
    if drift >= 3.0:
        new_lrc, last = retime_lrc(cues, anchors)
        if last <= audio_dur + 8:
            return {**base, "mode": "retimed", "lrc": new_lrc}
        return {**base, "mode": "retimed_clamp", "lrc": new_lrc, "warn": "last cue melewati durasi"}
    return {**base, "mode": "offset", "offset": round(round(med * 4) / 4, 2)}

def main():
    tracks_file = sys.argv[1]
    tracks = json.load(open(tracks_file, encoding="utf-8"))
    label = os.environ.get("VPS_LABEL", "vps1")
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device=NADAKU_DEVICE, compute_type=NADAKU_COMPUTE, cpu_threads=4)
    done, total = 0, len(tracks)
    for tr in tracks:
        outp = os.path.join(OUT, tr["id"] + ".json")
        if os.path.exists(outp):
            done += 1
            print(f"[{done}/{total}] SKIP {tr['id']}", flush=True)
            continue
        try:
            res = process(tr, model)
        except Exception as e:
            res = {"id": tr["id"], "mode": "error", "reason": str(e)[:150]}
        json.dump(res, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        done += 1
        print(f"[{done}/{total}] {tr['id']} mode={res.get('mode')} off={res.get('median_offset')} drift={res.get('drift_max')} | {str(res.get('reason',''))[:40]}", flush=True)
        # Checkpoint GitHub tiap 10 lagu selesai (aman dari kejadian tak terduga)
        token_file = os.environ.get("TOKEN_FILE", "/root/.github_token")
        if done % 10 == 0 and os.path.exists(token_file):
            try:
                r = subprocess.run(["bash", os.path.join(WORK, "checkpoint_push.sh"), label],
                                   capture_output=True, timeout=120)
                print("CHECKPOINT:", (r.stdout or b"").decode().strip()[-80:], flush=True)
            except Exception as e:
                print("CHECKPOINT gagal:", str(e)[:80], flush=True)
    print("PIPELINE DONE")

if __name__ == "__main__":
    main()
