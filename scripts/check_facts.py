"""Fact-map check: the numbers README.md and the submission draft state must equal their source of truth.

Sources of truth (stdlib only, read-only):
  ci       latest successful run of the `ci` workflow (`gh run view <id> --log`): contract tests (job `test`, the full
           `forge test` run), keeper tests (job `keeper`), scripts tests (job `scripts`). Offline: --ci-log FILE, or
           --contract-tests / --keeper-tests / --scripts-tests N (a flag wins over the log).
  status   newest status/rehearsal-*.md: step rows, rows whose Match is `yes`, its file name and block.
  out      contracts/out/PaidThrough.sol/PaidThrough.json: runtime and initcode size (fallback: the CI sizes table).
  deploy   keeper/deployments.json `mainnet` entry: address and txHash, once the deploy script has written them.

Each fact has context patterns ("{N} contract tests", "{N}-byte runtime", ...) where {N} accepts arabic numbers
(with or without thousands commas) and spelled-out ones ("thirty-seven"). A fact fails when
  CONFLICT  a different number sits in one of its contexts (e.g. "136 contract tests" while CI ran 138), or
  MISSING   a required fact appears 0 times across the docs (each doc, for the deployed address).
Guard-only facts (initcode size, rehearsal block) are not required to appear but may not be contradicted.

    python scripts/check_facts.py                      # gh + local files; exit 0 PASS, 1 FAIL
    python scripts/check_facts.py --contract-tests 138 --keeper-tests 68 --scripts-tests 18   # offline
    python scripts/check_facts.py --bait               # bumps each found number in temp copies; exit 0 if it bit
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCS = [ROOT / "README.md"]  # add more documents with --docs
DEFAULT_REPO = "bongbongcrypto/paidthrough"
USDC = "0x3600000000000000000000000000000000000000"

_SMALL = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen").split()
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_WORD = "(?:" + "|".join(sorted(_SMALL + list(_TENS) + ["hundred", "thousand"], key=len, reverse=True)) + r")\b"
NUM = r"(?P<n>\d{1,3}(?:,\d{3})+\b|\d+\b|" + _WORD + r"(?:(?:[ -]+|\s+and\s+)" + _WORD + ")*)"


def words_to_int(s: str) -> int | None:
    total = cur = 0
    for w in re.split(r"[\s-]+", s.lower().strip()):
        if w in ("", "and"):
            continue
        if w in _SMALL:
            cur += _SMALL.index(w)
        elif w in _TENS:
            cur += _TENS[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w == "thousand":
            total, cur = total + (cur or 1) * 1000, 0
        else:
            return None
    return total + cur


def to_int(s: str) -> int | None:
    s = s.strip()
    return int(s.replace(",", "")) if s[:1].isdigit() else words_to_int(s)


@dataclass
class Fact:
    name: str
    value: int | str
    source: str
    patterns: list[str]  # numeric: regex with {N}; text: regex whose group 1 must equal the value
    required: bool = True
    each_doc: bool = False
    ignore: tuple[str, ...] = ()


def build_facts(truth: dict) -> list[Fact]:
    facts = []
    truth = dict(truth)
    sources = truth.get("sources", {})

    def add(key, *args, **kw):
        if truth.get(key) is not None:
            facts.append(Fact(key, truth[key], *args, **kw))

    add("contract_tests", sources.get("contract_tests", "CI job test"), [r"\b{N}\s+(?:Foundry\s+)?contract\s+tests\b"])
    add("keeper_tests", sources.get("keeper_tests", "CI job keeper"),
        [r"\bkeeper\s+has\s+{N}\s+tests\b", r"\b{N}\s+keeper\s+tests\b", r"\bkeeper\b[^\n|]*?\(\s*{N}\s+tests\)"])
    add("scripts_tests", sources.get("scripts_tests", "CI job scripts"),
        [r"\b{N}\s+(?:[\w-]+\s+){0,3}?[\w-]*scripts?\s+tests\b",
         r"\bscripts\s+(?:job\s+)?(?:has|have|runs?)\s+{N}\s+tests\b"])
    rh = truth.get("rehearsal") or {}
    src = "status/" + rh.get("file", "?")
    for key in ("steps", "matched", "block"):
        truth["rehearsal_" + key] = rh.get(key)
    truth["rehearsal_file"] = rh.get("file")
    # "37-step rehearsal", "37 steps", "37 asserted steps", "all 37 rehearsal steps matched", "31 of 37 steps matched"
    add("rehearsal_steps", src + " step rows", [r"\b{N}[- ]step\b", r"\b{N}\s+(?:[\w-]+\s+)?steps\b"])
    add("rehearsal_matched", src + " rows matched",
        [r"\b{N}\s*(?:/|of)\s*\d[\d,]*\s+(?:[\w-]+\s+)?steps\s+(?:have\s+|all\s+)?matched\b",
         r"(?<!of )(?<!/)\b{N}\s+(?:[\w-]+\s+)?steps\s+(?:have\s+|all\s+)?matched\b"])
    add("rehearsal_file", src + " (rehearsal of record)", [r"(rehearsal-\d{4}-\d{2}-\d{2}\.md)"])
    add("rehearsal_block", src + " block", [r"rehearsal-\d{4}-\d{2}-\d{2}\.md\W{0,3}\s*(?:at\s+)?block\s+{N}"],
        required=False)
    size_src = sources.get("size", "contracts/out")
    for key, words in (("runtime_size", r"runtime"), ("initcode_size", r"(?:initcode|init\s+code|creation\s+code)")):
        add(key, size_src, [r"\b{N}[- ]byte\s+" + words + r"\b",
                            r"\b" + words + r"\s+(?:size\s+)?(?:of\s+|is\s+)?{N}\s+bytes\b",
                            r"\b{N}\s+bytes?\s+(?:of\s+)?" + words + r"\b"], required=(key == "runtime_size"))
    dep = truth.get("deployment") or {}
    truth["deploy_address"], truth["deploy_tx"] = dep.get("address"), dep.get("txHash")
    add("deploy_address", "keeper/deployments.json", [r"/address/(0x[0-9a-fA-F]{40})\b"], each_doc=True,
        ignore=(USDC,))
    add("deploy_tx", "keeper/deployments.json", [])
    return facts


def check(facts: list[Fact], docs: dict[str, str]) -> tuple[list[str], list[str]]:
    """Returns (report lines, problems). Each problem starts with the fact name."""
    report, problems = [], []
    for f in facts:
        hits = {d: 0 for d in docs}
        conflicts, seen = [], set()
        for dname, text in docs.items():
            if isinstance(f.value, str):
                hits[dname] = text.lower().count(f.value.lower())
            for pat in f.patterns:
                numeric = "{N}" in pat
                for m in re.finditer(pat.replace("{N}", NUM) if numeric else pat, text, re.I):
                    span = (dname, m.start("n") if numeric else m.start(1))
                    if span in seen:
                        continue
                    seen.add(span)
                    got = to_int(m.group("n")) if numeric else m.group(1).lower()
                    want = f.value if numeric else str(f.value).lower()
                    if numeric and got == want:
                        hits[dname] += 1
                    elif got != want and got not in [i.lower() for i in f.ignore]:
                        line = text.count("\n", 0, m.start()) + 1
                        conflicts.append(f"{dname}:{line} '{' '.join(m.group(0).split())}' gives {got}")
        total = sum(hits.values())
        missing = [d for d, n in hits.items() if n == 0] if f.each_doc else ([] if total else list(docs))
        status = "ok"
        if conflicts:
            status = "CONFLICT"
            problems.append(f"{f.name}: expected {f.value} ({f.source}); " + "; ".join(conflicts))
        if f.required and missing:
            status = "MISSING" if status == "ok" else status + "+MISSING"
            problems.append(f"{f.name}: {f.value} ({f.source}) appears 0 times in {', '.join(missing)}")
        kind = "required" if f.required else "guard"
        per_doc = ", ".join(f"{d} {n}" for d, n in hits.items())
        report.append(f"[{status:>8}] {f.name} = {f.value}  [{kind}; {f.source}]  hits: {per_doc}")
    return report, problems


# ---------------------------------------------------------------------------------------------- sources


def parse_ci_log(log: str) -> dict:
    out, contract = {}, []
    for line in log.splitlines():
        job = line.split("\t", 1)[0].strip()
        if job == "test":
            m = re.search(r"Ran \d+ test suites? in .*?: (\d+) tests passed", line)
            if m:
                contract.append(int(m.group(1)))
            m = re.search(r"\|\s*PaidThrough\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|", line)
            if m and "runtime_size" not in out:
                out["runtime_size"], out["initcode_size"] = (int(g.replace(",", "")) for g in m.groups())
        elif job in ("keeper", "scripts"):
            m = re.search(r"\bRan (\d+) tests? in [\d.]+s\b", line)
            if m:
                out[job + "_tests"] = int(m.group(1))
    if contract:
        out["contract_tests"] = max(contract)  # the full run; the gas-report run is a subset
    return out


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=True).stdout


def fetch_ci_log(repo: str, run_id: str | None) -> tuple[str, str]:
    if not run_id:
        runs = json.loads(gh("run", "list", "-R", repo, "-w", "ci", "-s", "success", "-L", "1",
                             "--json", "databaseId,headSha"))
        if not runs:
            raise SystemExit("no successful ci run found; pass --contract-tests/--keeper-tests/--scripts-tests")
        run_id = str(runs[0]["databaseId"])
        label = f"CI run {run_id} ({runs[0]['headSha'][:7]})"
    else:
        label = f"CI run {run_id}"
    return gh("run", "view", run_id, "-R", repo, "--log"), label


def parse_rehearsal(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    block = re.search(r"^Block (\d+)", text, re.M)
    steps = matched = 0
    col = None
    for ln in text.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells[:2] == ["#", "Step"] and "Match" in cells:
            col = cells.index("Match")
        elif col is not None and cells[0].isdigit():
            steps += 1
            matched += cells[col] == "yes"
    return {"file": path.name, "steps": steps, "matched": matched, "block": int(block.group(1)) if block else None}


def artifact_sizes(out_dir: Path) -> dict:
    p = out_dir / "PaidThrough.sol" / "PaidThrough.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    size = lambda o: len(o[2:] if o.startswith("0x") else o) // 2  # noqa: E731
    return {"runtime_size": size(d["deployedBytecode"]["object"]), "initcode_size": size(d["bytecode"]["object"])}


def gather_truth(a) -> dict:
    sources: dict = {}
    truth: dict = {"sources": sources}
    flags = {"contract_tests": a.contract_tests, "keeper_tests": a.keeper_tests, "scripts_tests": a.scripts_tests}
    ci: dict = {}
    label = "CI"
    if any(v is None for v in flags.values()) or a.ci_log:
        if a.ci_log:
            log, label = Path(a.ci_log).read_text(encoding="utf-8", errors="replace"), Path(a.ci_log).name
        else:
            log, label = fetch_ci_log(a.repo, a.run_id)
        ci = parse_ci_log(log)
    jobs = {"contract_tests": "test", "keeper_tests": "keeper", "scripts_tests": "scripts"}
    for k, v in flags.items():
        truth[k] = v if v is not None else ci.get(k)
        sources[k] = "--" + k.replace("_", "-") + " flag" if v is not None else f"{label} job {jobs[k]}"
    sizes = artifact_sizes(Path(a.out))
    sources["size"] = "contracts/out" if sizes else label + " sizes table"
    for k in ("runtime_size", "initcode_size"):
        truth[k] = sizes.get(k, ci.get(k))
    reh = Path(a.rehearsal) if a.rehearsal else max(Path(a.status_dir).glob("rehearsal-*.md"), default=None)
    truth["rehearsal"] = parse_rehearsal(reh) if reh else None
    dep_path = Path(a.deployments)
    if dep_path.exists():
        truth["deployment"] = (json.loads(dep_path.read_text(encoding="utf-8")) or {}).get(a.network)
    return truth


def bait(facts: list[Fact], doc_paths: list[Path]) -> int:
    """Bumps each fact found in the docs (one at a time, in temp copies) and expects a new problem for it.
    A number becomes number + 1; a text value (file name, address, tx hash) gets its last digit bumped."""
    with tempfile.TemporaryDirectory() as tmp:
        copies = {}
        for i, p in enumerate(doc_paths):
            copies[p.name] = Path(tmp) / f"{i}-{p.name}"
            shutil.copyfile(p, copies[p.name])
        texts = {n: c.read_text(encoding="utf-8") for n, c in copies.items()}
        _, base = check(facts, texts)
        tried = bit = 0
        for f in facts:
            if isinstance(f.value, int):
                hit = next(((n, m.start("n"), m.end("n")) for n, t in texts.items() for pat in f.patterns
                            for m in re.finditer(pat.replace("{N}", NUM), t, re.I)
                            if to_int(m.group("n")) == f.value), None)
            else:
                hit = next(((n, t.lower().find(f.value.lower()), t.lower().find(f.value.lower()) + len(f.value))
                            for n, t in texts.items() if f.value.lower() in t.lower()), None)
            if not hit:
                continue
            name, start, end = hit
            old = texts[name][start:end]
            if isinstance(f.value, int):
                new = f"{f.value + 1:,}" if "," in old else str(f.value + 1)
            else:
                i = max((j for j, c in enumerate(old) if c.isdigit()), default=None)
                if i is None:
                    continue
                new = old[:i] + str((int(old[i]) + 1) % 10) + old[i + 1:]
            copies[name].write_text(texts[name][:start] + new + texts[name][end:], encoding="utf-8")
            _, after = check(facts, {n: c.read_text(encoding="utf-8") for n, c in copies.items()})
            copies[name].write_text(texts[name], encoding="utf-8")
            fired = [p for p in after if p not in base and p.startswith(f.name + ":")]
            tried += 1
            bit += bool(fired)
            print(f"[{'bit' if fired else 'MISSED'}] {f.name}: '{old}' -> '{new}' in {name}"
                  + (f" -> {fired[0]}" if fired else ""))
    print(f"BAIT: {bit}/{tried} bumped facts caught" + ("" if tried else " (nothing found to bump)"))
    return 0 if tried and bit == tried else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--docs", nargs="+", default=[str(p) for p in DEFAULT_DOCS])
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--run-id", help="ci run to read (default: latest successful)")
    ap.add_argument("--ci-log", help="saved `gh run view <id> --log` output instead of calling gh")
    ap.add_argument("--contract-tests", type=int)
    ap.add_argument("--keeper-tests", type=int)
    ap.add_argument("--scripts-tests", type=int)
    ap.add_argument("--out", default=str(ROOT / "contracts" / "out"))
    ap.add_argument("--status-dir", default=str(ROOT / "status"))
    ap.add_argument("--rehearsal", help="rehearsal markdown to use instead of the newest status/rehearsal-*.md")
    ap.add_argument("--deployments", default=str(ROOT / "keeper" / "deployments.json"))
    ap.add_argument("--network", default="mainnet")
    ap.add_argument("--bait", action="store_true", help="prove the check bites on temp copies of the docs")
    a = ap.parse_args(argv)

    doc_paths = [Path(d) for d in a.docs]
    absent = [str(p) for p in doc_paths if not p.exists()]
    if absent:
        print("missing doc(s): " + ", ".join(absent))
        return 1
    truth = gather_truth(a)
    facts = build_facts(truth)
    registered = {f.name for f in facts}
    unregistered = [k for k in ("contract_tests", "keeper_tests", "scripts_tests", "rehearsal_steps",
                                "rehearsal_matched", "rehearsal_file", "runtime_size") if k not in registered]
    if a.bait:
        return bait(facts, doc_paths)
    report, problems = check(facts, {p.name: p.read_text(encoding="utf-8") for p in doc_paths})
    print("\n".join(report))
    if not truth.get("deploy_address"):
        print(f"(not deployed: keeper/deployments.json has no {a.network} entry; address/txHash not checked)")
    for k in unregistered:
        problems.append(f"{k}: no source of truth found")
    for p in problems:
        print("PROBLEM " + p)
    print(f"FACTS: {'PASS' if not problems else 'FAIL'} - {len(facts)} facts, {len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
