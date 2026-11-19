"""Robustness check: run every stage of the tool over every Power BI model found under a folder.

    python scripts/robustness_check.py <folder with one or many PBIP projects> [--verbose]

Use it before a rollout to see, for your own models: whether every model/report is readable,
which source tables are recognised, how many measures translate to SQL automatically (and why
the others do not), and that no stage crashes. Read-only: nothing is executed or modified.
"""
from __future__ import annotations

import collections
import json
import sys
import traceback
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analyzers.dax import DaxAnalyzer, tokenize                         # noqa: E402
from app.analyzers.dax_infer import infer_kpi                                # noqa: E402
from app.core.project import build_mapping, create_project, infer_kpis       # noqa: E402
from app.engine.sql_generator import MappingError, kpi_value_sql             # noqa: E402
from app.engine.test_generator import generate_tests                         # noqa: E402
from app.metadata.pbip import read_pbip                                      # noqa: E402


def model_roots(corpus: Path) -> list[Path]:
    roots = set()
    for p in corpus.rglob("*"):
        if p.is_dir() and (p.name.endswith(".SemanticModel") or p.name.endswith(".Dataset")):
            roots.add(p)
    for p in corpus.rglob("definition/tables"):
        if not any(r in p.parents for r in roots):
            roots.add(p.parent)
    for p in corpus.rglob("*.bim"):
        if not any(r in p.parents or r == p.parent for r in roots):
            roots.add(p.parent)
    return sorted(roots)


def main(corpus: str, verbose: bool):
    stats = collections.Counter()
    crashes = collections.defaultdict(list)
    rules = collections.Counter()
    untrans = collections.Counter()
    samples = collections.defaultdict(list)
    for root in model_roots(Path(corpus)):
        stats["models"] += 1
        try:
            model = read_pbip(root.parent if root.name.endswith((".SemanticModel", ".Dataset")) else root)
        except Exception as e:  # noqa: BLE001
            crashes["read"].append(f"{root}: {type(e).__name__}: {str(e)[:120]}")
            continue
        stats["models_read"] += 1
        stats["tables"] += len(model.tables)
        stats["measures"] += len(model.all_measures())
        stats["visuals"] += len(model.visuals)
        stats["rels"] += len(model.relationships)
        for m in model.all_measures():
            if not m.expression.strip():
                samples["empty_expr"].append(f"{root.name}:{m.name}")
            if "formatString:" in m.expression or "lineageTag:" in m.expression:
                samples["prop_leak"].append(f"{root.name}:{m.name}")
        try:
            an = DaxAnalyzer(model)
        except Exception as e:  # noqa: BLE001
            crashes["analyzer_init"].append(f"{root}: {e}")
            continue
        for m in model.all_measures():
            try:
                f = an.static(m.name, m.expression)
                for x in f:
                    rules[(x.rule, x.severity)] += 1
                    if x.severity == "error" and len(samples[x.rule]) < 25:
                        samples[x.rule].append(f"{root.name} [{m.name}]: {x.message} || {m.expression[:160]!r}")
            except Exception as e:  # noqa: BLE001
                crashes["static"].append(f"{root.name}:{m.name}: {type(e).__name__}: {e}")
            try:
                exp, _ = an.expand(m.expression, (m.name.lower(),))
                mk, _ = an.expand(m.expression, (m.name.lower(),), mark=True)
                k = infer_kpi(m, exp, "K", model, marked=mk)
                ok = bool(k.all_specs())
                stats["translated"] += ok
                if not ok:
                    note = next((n for n in k.translation_notes if n.startswith("Not auto")), "?")
                    key = note.split("]")[0] + "] " + note.split("]: ")[-1][:60] if "]" in note else note
                    untrans[key] += 1
                    if len(samples["untrans:" + key]) < 4:
                        samples["untrans:" + key].append(f"{root.name} [{m.name}]: {m.expression[:200]!r}")
            except Exception as e:  # noqa: BLE001
                crashes["infer"].append(f"{root.name}:{m.name}: {type(e).__name__}: {e} {traceback.format_exc()[-300:]}")
        try:
            env, notes = create_project(model, root)
            kpis = infer_kpis(model)
            for k in kpis:
                if k.all_specs():
                    try:
                        kpi_value_sql(k, env, env.default_context.filters_for(k), allow_draft=True)
                        stats["sql_ok"] += 1
                        for g in k.grains:
                            kpi_value_sql(k, env, [], grain=g, allow_draft=True)
                            stats["grain_sql_ok"] += 1
                    except MappingError as e:
                        stats["sql_mapping_blocked"] += 1
                        if len(samples["sqlblock"]) < 25:
                            samples["sqlblock"].append(f"{root.name} [{k.name}]: {e}")
                    except Exception as e:  # noqa: BLE001
                        crashes["sql"].append(f"{root.name}:{k.name}: {type(e).__name__}: {e}")
            analyses = an.analyze(env, kpis)
            tests = generate_tests(env, kpis, model, analyses, [])
            stats["tests"] += len(tests)
            from app.engine.runner import ExecContext, execute_test
            ctx = ExecContext(env=env, bi=None, data=None, kpis={k.id: k for k in kpis}, model=model,
                              analyses={a.measure.lower(): a for a in analyses})
            for t in tests:
                r = execute_test(ctx, t, "H")
                stats["status:" + r.status.value] += 1
                if r.status.value == "ERROR":
                    crashes["run"].append(f"{root.name}:{t.id}: {r.message[:150]}")
                if t.executor == "visual_binding" and r.status.value == "FAIL" and len(samples["visfail"]) < 15:
                    samples["visfail"].append(f"{root.name}:{t.name}: {r.message[:150]}")
            src = collections.Counter(t.source_kind for t in model.tables)
            for kk, v in src.items():
                stats[f"src:{kk}"] += v
        except Exception as e:  # noqa: BLE001
            crashes["project"].append(f"{root.name}: {type(e).__name__}: {e} {traceback.format_exc()[-400:]}")
    print(json.dumps(dict(stats), indent=1))
    print("translation coverage:", f"{stats['translated']}/{stats['measures']}")
    print("\nRULES:", dict(rules))
    print("\nUNTRANSLATABLE:", untrans.most_common(25))
    for k, v in crashes.items():
        print(f"\nCRASH {k}: {len(v)}")
        for x in v[:8]:
            print("  ", x)
    for k, v in samples.items():
        if verbose or not k.startswith("untrans:"):
            print(f"\nSAMPLE {k}: {len(v)}")
            for x in v[:10]:
                print("  ", x[:300])


if __name__ == "__main__":
    main(sys.argv[1], "--verbose" in sys.argv)
