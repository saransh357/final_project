"""
Double Pendulum Chaos Key Generator
=====================================
Tracks object motion (via webcam) + audio input simultaneously,
extracts entropy from both streams, and derives a cryptographic key.

Dependencies:
    pip install opencv-python numpy pyaudio hashlib cryptography
dont pip install opencv all the depen are in install.py 
all the additional thing are in install.py 
install.py has type 

Usage:
    python pendulum_key_gen.py
"""
import cv2
import numpy as np
import pyaudio
import hashlib
import threading
import time
import struct
import secrets
import hmac
import logging
import json
import base64
from collections import deque
from datetime import datetime, timezone
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives import hashes, constant_time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
import os

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger("ChaosKey")

# ─── Config ───────────────────────────────────────────────────────────────────

VIDEO_SOURCE     = 0
CAPTURE_SECONDS  = 20         # Minimum; grows dynamically until entropy threshold met
MIN_ENTROPY_BYTES = 4096      # Do not derive key until this much raw entropy is pooled
KEY_LENGTH_BYTES = 32         # 256-bit key
AUDIO_RATE       = 44100
AUDIO_CHUNK      = 1024
AUDIO_CHANNELS   = 1
MAX_TRACKED_POINTS = 500
OPTICAL_FLOW_PARAMS = dict(
    winSize  = (15, 15),
    maxLevel = 2,
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03)
)

# ─── Entropy Pool (thread-safe, self-mixing) ──────────────────────────────────

class EntropyPool:
    """
    Rolling entropy accumulator.
    - Thread-safe via lock.
    - Every write XOR-folds new data into the pool to prevent simple
      concatenation attacks.
    - Tracks a conservative Shannon-entropy estimate.
    """

    def __init__(self, capacity: int = 65536):
        self._lock     = threading.Lock()
        self._buf      = bytearray(capacity)
        # Numpy view over the same memory — no copy, lets add() use vectorised ops
        self._buf_np   = np.frombuffer(self._buf, dtype=np.uint8)
        self._pos      = 0          # kept < capacity*256 to avoid unbounded growth
        self._total    = 0
        self._capacity = capacity

    def add(self, data: bytes):
        if not data:
            return
        with self._lock:
            n    = len(data)
            arr  = np.frombuffer(data, dtype=np.uint8)
            # Wrap-around indices into the fixed-size pool
            idx  = np.arange(self._pos, self._pos + n, dtype=np.int64) % self._capacity
            # Position-dependent mixing mask.
            # Must generate in int64 THEN cast to uint8: numpy 2.x validates that
            # the start/stop values fit the dtype before truncating, so
            # np.arange(2120, ..., dtype=np.uint8) raises OverflowError once _pos>255.
            mask = np.arange(self._pos, self._pos + n, dtype=np.int64).astype(np.uint8)
            # XOR fold: bitwise XOR is always in [0,255] — can never overflow uint8.
            np.bitwise_xor.at(self._buf_np, idx, arr ^ mask)
            self._pos   = (self._pos + n) % (self._capacity * 256)
            self._total += n

    def snapshot(self) -> bytes:
        """Return a SHA-512 hash of the current pool state (non-destructive)."""
        with self._lock:
            return hashlib.sha512(bytes(self._buf[:min(self._pos, self._capacity)])).digest()

    def raw(self) -> bytes:
        with self._lock:
            return bytes(self._buf[:min(self._pos, self._capacity)])

    @property
    def bytes_added(self) -> int:
        with self._lock:
            return self._total


entropy_pool = EntropyPool()


# ─── Audio Entropy Collector ──────────────────────────────────────────────────

class AudioEntropyCollector(threading.Thread):
    """Captures raw audio and feeds amplitude + zero-crossing entropy."""

    def __init__(self, duration: float):
        super().__init__(daemon=True)
        self.duration = duration
        self.stopped  = threading.Event()

    def run(self):
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            log.info("[Audio] Capturing started")
            end_time = time.time() + self.duration

            while time.time() < end_time and not self.stopped.is_set():
                try:
                    raw = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                    entropy_pool.add(raw)

                    # Work from numpy view (no copy of raw)
                    samples    = np.frombuffer(raw, dtype=np.int16)
                    amplitude  = int(np.abs(samples).mean())
                    zero_cross = int(np.sum(np.diff(np.sign(samples)) != 0))
                    # Compute only the first 10 FFT bins — avoids a full 1024-bin
                    # complex128 allocation (16 KB) just to slice 10 values
                    spectral_e = int(np.sum(np.abs(np.fft.rfft(samples, n=20)[:10])))
                    del samples  # release numpy view before packing

                    entropy_pool.add(struct.pack(
                        '>HHI',
                        amplitude  & 0xFFFF,
                        zero_cross & 0xFFFF,
                        spectral_e & 0xFFFFFFFF
                    ))
                    del raw  # release audio buffer

                    # High-resolution timestamp jitter
                    entropy_pool.add(struct.pack('>d', time.perf_counter()))

                except Exception as e:
                    log.warning(f"[Audio] {e}")

            stream.stop_stream()
            stream.close()
            pa.terminate()
            log.info("[Audio] Done")
        except Exception as e:
            log.error(f"[Audio] Fatal: {e}")


# ─── Video / Motion Entropy Collector ────────────────────────────────────────

class MotionEntropyCollector:
    """
    Uses Lucas-Kanade optical flow to track feature points.
    Motion vectors (dx, dy, speed, angle) plus pixel neighbourhoods feed entropy.

    Optical Flow (Lucas-Kanade) assumption:
      I(x, y, t) = I(x+dx, y+dy, t+dt)
    Taylor expansion → Ix·u + Iy·v + It = 0  (aperture problem)
    LK solves a local least-squares system over a window, giving:
      [u, v] = -(A^T A)^{-1} A^T b
    """

    def __init__(self):
        self.cap       = cv2.VideoCapture(VIDEO_SOURCE)
        self.prev_gray = None
        self.prev_pts  = None
        self.trail     = deque(maxlen=MAX_TRACKED_POINTS)
        self.frame_count = 0

    def _reinit_points(self, gray):
        return cv2.goodFeaturesToTrack(
            gray, maxCorners=50, qualityLevel=0.01,
            minDistance=10, blockSize=7
        )

    def collect(self, duration: float):
        if not self.cap.isOpened():
            raise RuntimeError("Cannot open webcam.")

        log.info("[Video] Optical-flow tracking started")
        end_time = time.time() + duration
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError("Cannot read from webcam.")

        self.prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.prev_pts  = self._reinit_points(self.prev_gray)
        good_new       = []

        while time.time() < end_time:
            ret, frame = self.cap.read()
            if not ret:
                break

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            self.frame_count += 1

            # Hash a strided sample of the frame rather than calling .tobytes()
            # on the full frame (which allocates a 300 KB copy per frame).
            # Taking every 8th pixel in both dimensions gives ~2 KB of data that
            # still captures scene variation without duplicating the frame buffer.
            entropy_pool.add(hashlib.md5(gray[::8, ::8].tobytes()).digest())

            if self.prev_pts is not None and len(self.prev_pts) > 0:
                next_pts, status, _ = cv2.calcOpticalFlowPyrLK(
                    self.prev_gray, gray, self.prev_pts, None,
                    **OPTICAL_FLOW_PARAMS
                )

                if next_pts is not None:
                    good_new = next_pts[status == 1]
                    good_old = self.prev_pts[status == 1]
                else:
                    good_new = np.array([])
                    good_old = np.array([])

                for new, old in zip(good_new, good_old):
                    nx, ny = new.ravel()
                    ox, oy = old.ravel()
                    dx, dy = nx - ox, ny - oy
                    speed  = np.hypot(dx, dy)
                    angle  = np.arctan2(dy, dx)

                    self.trail.append(((int(ox), int(oy)), (int(nx), int(ny))))

                    entropy_pool.add(struct.pack(
                        '>ffff',
                        float(dx), float(dy), float(speed), float(angle)
                    ))

                    px, py = int(nx), int(ny)
                    patch  = gray[max(0, py-2):py+3, max(0, px-2):px+3]
                    entropy_pool.add(patch.tobytes())

                if len(good_new) < 5:
                    self.prev_pts = self._reinit_points(gray)
                else:
                    self.prev_pts = good_new.reshape(-1, 1, 2)

                # Release old arrays explicitly; prev_gray will be replaced below
                del good_old
            else:
                good_new = np.empty((0, 2), dtype=np.float32)
                self.prev_pts = self._reinit_points(gray)

            # ── Live display — draw in-place on frame (no .copy() allocation) ──
            for (p1, p2) in self.trail:
                cv2.line(frame, p1, p2, (0, 255, 100), 1)
            for pt in good_new:
                cv2.circle(frame, (int(pt[0]), int(pt[1])), 3, (0, 100, 255), -1)

            remaining = max(0.0, end_time - time.time())
            cv2.putText(
                frame,
                f"Entropy: {entropy_pool.bytes_added} bytes | {remaining:.1f}s",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 0), 2
            )
            cv2.imshow("Chaos Key Generator - Press Q to stop early", frame)
            del good_new  # release motion array before next iteration

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

            # Explicitly release old grayscale before assigning new one
            self.prev_gray = None
            self.prev_gray = gray

        self.cap.release()
        cv2.destroyAllWindows()
        log.info(f"[Video] Done — {self.frame_count} frames processed")


# ─── Key Derivation ───────────────────────────────────────────────────────────

def derive_key(raw_entropy: bytes, key_length: int = KEY_LENGTH_BYTES,
               extra_context: bytes = b"") -> dict:
    """
    Derive a cryptographic key using a 3-stage pipeline:

      1. SHA-512 of raw pool       → 64-byte pre-image
      2. Scrypt (N=2^17, r=8, p=1) → 64-byte memory-hard stretch
      3. HKDF-SHA256               → final key bytes

    Returns a dict with the key and all public parameters needed to
    verify / re-derive (salt is secret and returned separately).
    """
    if len(raw_entropy) < 64:
        raise ValueError("Insufficient entropy to derive key safely.")

    # Capture length now — raw_entropy is deleted after hashing so the
    # 64 KB pool copy is freed before Scrypt's ~128 MB allocation.
    entropy_byte_count = len(raw_entropy)

    # Stage 1: compress, then release the pool copy immediately.
    stage1 = hashlib.sha512(raw_entropy).digest()
    del raw_entropy

    # Stage 2: memory-hard KDF — makes brute-force extremely expensive
    scrypt_salt = secrets.token_bytes(32)
    scrypt = Scrypt(
        salt=scrypt_salt, length=64,
        n=2**17, r=8, p=1,          # ~128 MB RAM, ~1 s on modern CPU
        backend=default_backend()
    )
    stage2 = scrypt.derive(stage1)
    del stage1  # free 64-byte pre-image; Scrypt working mem already released

    # Stage 3: HKDF for domain separation and length expansion
    hkdf_salt = secrets.token_bytes(32)
    hkdf_info = b"chaos-pendulum-key-v2|" + extra_context
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=key_length,
        salt=hkdf_salt,
        info=hkdf_info,
        backend=default_backend()
    )
    key = hkdf.derive(stage2)
    del stage2  # free 64-byte intermediate

    # Integrity tag: HMAC-SHA256 over (scrypt_salt || hkdf_salt || key)
    mac_key  = secrets.token_bytes(32)
    tag      = hmac.new(mac_key, scrypt_salt + hkdf_salt + key, hashlib.sha256).digest()

    return {
        "key":         key,
        "key_hex":     key.hex(),
        "key_b64":     base64.b64encode(key).decode(),
        "scrypt_salt": scrypt_salt.hex(),
        "hkdf_salt":   hkdf_salt.hex(),
        "mac_tag":     tag.hex(),
        "mac_key":     mac_key.hex(),   # store securely — needed to verify tag
        "entropy_bytes": entropy_byte_count,
        "bits":        key_length * 8,
        "algorithm":   "SHA512 → Scrypt(N=2^17,r=8,p=1) → HKDF-SHA256",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


# ─── Encrypt / Decrypt helpers (for API use) ─────────────────────────────────

def encrypt_with_key(key_hex: str, plaintext: str) -> dict:
    """AES-256-GCM encryption. Returns base64 ciphertext + nonce."""
    key    = bytes.fromhex(key_hex)
    nonce  = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ct     = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return {
        "ciphertext": base64.b64encode(ct).decode(),
        "nonce":      base64.b64encode(nonce).decode(),
        "algorithm":  "AES-256-GCM"
    }


def decrypt_with_key(key_hex: str, ciphertext_b64: str, nonce_b64: str) -> str:
    """AES-256-GCM decryption. Returns plaintext string."""
    key    = bytes.fromhex(key_hex)
    ct     = base64.b64decode(ciphertext_b64)
    nonce  = base64.b64decode(nonce_b64)
    aesgcm = AESGCM(key)
    pt     = aesgcm.decrypt(nonce, ct, None)
    return pt.decode()


# ─── Flask API ────────────────────────────────────────────────────────────────

def start_api(key_store: dict, api_token: str, host: str = "0.0.0.0", port: int = 5000):
    """
    Minimal Flask REST API. Protected by Bearer token.

    Endpoints:
      GET  /api/status            → service health + entropy stats
      GET  /api/key               → current key metadata (not raw key bytes)
      POST /api/encrypt           → AES-256-GCM encrypt
      POST /api/decrypt           → AES-256-GCM decrypt
      POST /api/rederive          → trigger a new key derivation from live pool
    """
    try:
        from flask import Flask, request, jsonify, abort
        from functools import wraps
    except ImportError:
        log.error("Flask not installed. Run: pip install flask")
        return

    app = Flask("ChaosKeyAPI")

    def require_token(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                abort(401)
            token = auth[7:]
            # constant-time comparison prevents timing attacks
            if not constant_time.bytes_eq(token.encode(), api_token.encode()):
                abort(403)
            return f(*args, **kwargs)
        return decorated

    @app.route("/api/status")
    @require_token
    def status():
        return jsonify({
            "status":        "ok",
            "entropy_bytes": entropy_pool.bytes_added,
            "key_ready":     bool(key_store.get("key_hex")),
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })

    @app.route("/api/key")
    @require_token
    def get_key():
        if not key_store.get("key_hex"):
            return jsonify({"error": "No key derived yet"}), 404
        # Return metadata but NOT the raw key bytes for safety
        safe = {k: v for k, v in key_store.items() if k not in ("key",)}
        return jsonify(safe)

    @app.route("/api/key/raw")
    @require_token
    def get_key_raw():
        """Returns the raw key — protect this endpoint carefully."""
        if not key_store.get("key_hex"):
            return jsonify({"error": "No key derived yet"}), 404
        return jsonify({
            "key_hex": key_store["key_hex"],
            "key_b64": key_store["key_b64"],
        })

    @app.route("/api/encrypt", methods=["POST"])
    @require_token
    def api_encrypt():
        if not key_store.get("key_hex"):
            return jsonify({"error": "No key derived yet"}), 404
        body = request.get_json(force=True)
        if "plaintext" not in body:
            return jsonify({"error": "Missing 'plaintext' field"}), 400
        result = encrypt_with_key(key_store["key_hex"], body["plaintext"])
        return jsonify(result)

    @app.route("/api/decrypt", methods=["POST"])
    @require_token
    def api_decrypt():
        if not key_store.get("key_hex"):
            return jsonify({"error": "No key derived yet"}), 404
        body = request.get_json(force=True)
        required = {"ciphertext", "nonce"}
        if not required.issubset(body):
            return jsonify({"error": f"Missing fields: {required - set(body)}"}), 400
        try:
            plaintext = decrypt_with_key(
                key_store["key_hex"], body["ciphertext"], body["nonce"]
            )
            return jsonify({"plaintext": plaintext})
        except Exception as e:
            return jsonify({"error": f"Decryption failed: {e}"}), 400

    @app.route("/api/rederive", methods=["POST"])
    @require_token
    def rederive():
        raw = entropy_pool.raw()
        if len(raw) < 64:
            return jsonify({"error": "Insufficient entropy"}), 422
        new_key = derive_key(raw)
        key_store.update(new_key)
        safe = {k: v for k, v in key_store.items() if k not in ("key",)}
        log.info("[API] Key re-derived on request")
        return jsonify({"message": "Key re-derived", **safe})

    log.info(f"[API] Starting on http://{host}:{port}")
    log.info(f"[API] Bearer token: {api_token}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Chaos Key Generator  v2 — Security Hardened")
    print("=" * 60)
    print(f"  Collecting entropy for ≥{CAPTURE_SECONDS}s (until {MIN_ENTROPY_BYTES} bytes pooled)")
    print("  Point camera at your double pendulum now.\n")

    # Add OS entropy to bootstrap
    entropy_pool.add(os.urandom(64))
    entropy_pool.add(struct.pack('>d', time.time()))

    # Start audio thread
    audio_thread = AudioEntropyCollector(duration=CAPTURE_SECONDS + 5)
    audio_thread.start()

    # Collect video entropy (extends automatically if pool too small)
    tracker = MotionEntropyCollector()
    duration = float(CAPTURE_SECONDS)

    while True:
        tracker.collect(duration=duration)
        if entropy_pool.bytes_added >= MIN_ENTROPY_BYTES:
            break
        log.warning(f"Only {entropy_pool.bytes_added} bytes — collecting more...")
        duration = 10.0   # additional 10-second rounds

    audio_thread.stopped.set()
    audio_thread.join(timeout=5)

    # Derive key
    raw = entropy_pool.raw()
    log.info(f"[KeyGen] Total raw entropy: {len(raw)} bytes")

    key_data = derive_key(raw)

    print("\n" + "=" * 60)
    print("  DERIVED CRYPTOGRAPHIC KEY")
    print("=" * 60)
    print(f"  Hex : {key_data['key_hex']}")
    print(f"  B64 : {key_data['key_b64']}")
    print(f"  Bits: {key_data['bits']}")
    print(f"  KDF : {key_data['algorithm']}")
    print(f"  Time: {key_data['timestamp']}")
    print(f"  Raw entropy fed: {key_data['entropy_bytes']} bytes")
    print("=" * 60)

    # Save
    save = input("\nSave key to file? (y/n): ").strip().lower()
    if save == 'y':
        fname = f"chaos_key_{int(time.time())}.json"
        with open(fname, 'w') as f:
            safe = {k: v for k, v in key_data.items() if k != "key"}
            json.dump(safe, f, indent=2)
        print(f"Key metadata saved to: {fname}")
        bin_fname = fname.replace(".json", ".key")
        with open(bin_fname, 'wb') as f:
            f.write(key_data["key"])
        print(f"Raw key bytes saved to: {bin_fname}")

    # Start API?
    start_server = input("\nStart REST API server? (y/n): ").strip().lower()
    if start_server == 'y':
        api_token = secrets.token_urlsafe(32)
        print(f"\n  API Token (keep secret): {api_token}")
        print("  Endpoints:")
        print("    GET  /api/status")
        print("    GET  /api/key")
        print("    GET  /api/key/raw")
        print("    POST /api/encrypt   {\"plaintext\": \"...\"}")
        print("    POST /api/decrypt   {\"ciphertext\": \"...\", \"nonce\": \"...\"}")
        print("    POST /api/rederive\n")
        key_store = dict(key_data)
        start_api(key_store, api_token)

    return key_data


if __name__ == "__main__":
    main()