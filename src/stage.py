"""Write staged recipe records to the staging area, and track what we have seen.

Staging lives on thebigbox but the harvester runs on the Z4 G4, so writes go
over the existing key-based SSH. One SSH round trip per file would mean forty
for a normal weekly run, so records are buffered locally and shipped as a single
tar stream, with state written once at the end.

Pass ``host=None`` to write to a local directory instead. That is what the tests
use, and what ``--dry-run`` uses.

State files, all under ``state/``:

``seen.json``       canonical url -> {status, first_seen, source}
``blacklist.json``  canonical urls never to stage again
``cursors.json``    per-source high-water mark for the weekly "what's new" pass
"""

import base64
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from .canonical import canonicalize, record_id

# Only relevant if you drive the harvester from a different host than the
# one staging lives on; this app itself always passes host=None (local disk).
HOST = None
ROOT = "/data/staging"
SCHEMA = 1


class StageError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_record(candidate, entry, extracted, diet_tag_value) -> dict:
    """Assemble the on-disk record. Shape is locked - see INGESTION.md section 6."""
    url = canonicalize(candidate.url)
    rid = record_id(url)
    preview = dict(extracted["preview"])
    preview["image"] = f"{rid}.jpg" if extracted.get("image_url") else None
    if not preview.get("title") and candidate.title:
        preview["title"] = candidate.title

    return {
        "schema": SCHEMA,
        "id": rid,
        "canonical_url": url,
        "source": entry["domain"],
        "source_name": entry["name"],
        "staged_at": _now(),
        "diet": diet_tag_value,
        "preview": preview,
        "jsonld": extracted.get("jsonld"),
        "html": extracted.get("html"),
    }


class Stager:
    """Buffers records, then ships them in one shot.

    Use as a context manager so the flush always happens::

        with Stager() as st:
            if not st.is_known(url):
                st.stage(record, image_bytes)
    """

    def __init__(self, host: str | None = HOST, root: str | Path = ROOT):
        self.host = host
        self.root = Path(root)
        self._tmp = Path(tempfile.mkdtemp(prefix="recipe-stage-"))
        self.seen: dict = {}
        self.blacklist: dict = {}
        self.cursors: dict = {}
        self._staged: list[str] = []
        self._state_loaded = False

    # ---------- plumbing ----------

    def _run(self, argv: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, **kw)

    def _remote_python(self, script: str, stdin: bytes | None = None):
        """Run a python snippet on the staging host, avoiding all quoting pain.

        The script travels base64-encoded inside ``python3 -c`` rather than
        being piped in. Piping it would make the script itself python's stdin,
        leaving no way to hand the process a data payload.
        """
        blob = base64.b64encode(script.encode()).decode()
        cmd = f"python3 -c 'import base64;exec(base64.b64decode(\"{blob}\"))'"
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, cmd],
                              input=stdin, capture_output=True)
        if proc.returncode != 0:
            raise StageError(f"remote python failed: {proc.stderr.decode()[:400]}")
        return proc.stdout

    def load_state(self) -> None:
        """Read seen/blacklist/cursors in a single round trip."""
        names = ("seen", "blacklist", "cursors")
        if self.host is None:
            for name in names:
                path = self.root / "state" / f"{name}.json"
                setattr(self, name, json.loads(path.read_text()) if path.exists() else {})
        else:
            script = (
                "import json,os\n"
                f"root={str(self.root)!r}\n"
                "out={}\n"
                f"for n in {names!r}:\n"
                "    p=os.path.join(root,'state',n+'.json')\n"
                "    try:\n"
                "        out[n]=json.load(open(p))\n"
                "    except Exception:\n"
                "        out[n]={}\n"
                "print(json.dumps(out))\n"
            )
            data = json.loads(self._remote_python(script).decode())
            for name in names:
                setattr(self, name, data.get(name, {}))
        self._state_loaded = True

    def _require_state(self):
        if not self._state_loaded:
            self.load_state()

    # ---------- decisions ----------

    def is_known(self, url: str) -> bool:
        """True when this URL has been staged, imported, rejected, or blacklisted."""
        self._require_state()
        key = canonicalize(url)
        return key in self.seen or key in self.blacklist

    def note_skip(self, url: str, reason: str, source: str = "") -> None:
        """Record a URL we chose not to stage, so it is not reconsidered."""
        self._require_state()
        self.seen[canonicalize(url)] = {
            "status": f"skipped: {reason}", "first_seen": _now(), "source": source}

    # ---------- writing ----------

    def stage(self, record: dict, image_bytes: bytes | None = None) -> bool:
        """Buffer one record. Returns False if it was already known."""
        self._require_state()
        url = record["canonical_url"]
        if url in self.seen or url in self.blacklist:
            return False

        rid = record["id"]
        (self._tmp / f"{rid}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))
        if image_bytes:
            (self._tmp / f"{rid}.jpg").write_bytes(image_bytes)
        else:
            record["preview"]["image"] = None

        self.seen[url] = {"status": "pending", "first_seen": record["staged_at"],
                          "source": record["source"]}
        self._staged.append(rid)
        return True

    def set_cursor(self, source: str, value: str) -> None:
        self._require_state()
        if value:
            self.cursors[source] = value

    def flush(self) -> int:
        """Ship buffered files and write state. Safe to call with nothing buffered."""
        self._require_state()
        pending = self.root / "pending"

        files = sorted(p for p in self._tmp.iterdir())
        if files:
            if self.host is None:
                pending.mkdir(parents=True, exist_ok=True)
                for path in files:
                    (pending / path.name).write_bytes(path.read_bytes())
            else:
                buf = BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    for path in files:
                        tar.add(path, arcname=path.name)
                dest = shlex.quote(str(pending))
                proc = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", self.host,
                     f"mkdir -p {dest} && tar x -C {dest}"],
                    input=buf.getvalue(), capture_output=True)
                if proc.returncode != 0:
                    raise StageError(f"tar push failed: {proc.stderr.decode()[:400]}")

        self._write_state()
        return len(self._staged)

    def _write_state(self) -> None:
        """Atomic per-file: write a temp then rename, so a crash cannot truncate
        state and a concurrent cron/manual run cannot read a half-written file."""
        payload = {"seen": self.seen, "blacklist": self.blacklist, "cursors": self.cursors}
        if self.host is None:
            state = self.root / "state"
            state.mkdir(parents=True, exist_ok=True)
            for name, value in payload.items():
                tmp = state / f"{name}.json.tmp"
                tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False))
                os.replace(tmp, state / f"{name}.json")
            return

        script = (
            "import json,os,sys\n"
            f"root={str(self.root)!r}\n"
            "data=json.load(sys.stdin)\n"
            "d=os.path.join(root,'state')\n"
            "os.makedirs(d, exist_ok=True)\n"
            "for name,value in data.items():\n"
            "    tmp=os.path.join(d,name+'.json.tmp')\n"
            "    with open(tmp,'w') as f:\n"
            "        json.dump(value,f,indent=1,ensure_ascii=False)\n"
            "    os.replace(tmp, os.path.join(d,name+'.json'))\n"
            "print('ok')\n"
        )
        self._remote_python(script, stdin=json.dumps(payload).encode())

    def cleanup(self) -> None:
        for path in self._tmp.iterdir():
            path.unlink()
        self._tmp.rmdir()

    def __enter__(self):
        self._require_state()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.flush()
        self.cleanup()
        return False
