"""Keyword groups: run the pipeline (proposals), and the person's actions on them.

Proposals live in the session store. Only APPROVED groups are mirrored into the workspace's
DuckDB table `_kw_groups`, which is all the engine ever reads (D-B7-4).
"""
from __future__ import annotations

import json
import secrets

from backend.engine.analysis.shared_v2 import GROUP_MAP
from backend.engine.util import db
from backend.services.sessions import ServiceError

TEXT_CONCEPTS = ("search_term", "query", "keyword")


class KeywordService:
    def __init__(self, datasets, labeler=None):
        self.ds, self.labeler = datasets, labeler
        con = self.ds.store._con
        with self.ds.store._lock:
            con.execute("CREATE TABLE IF NOT EXISTS keyword_groups (dataset_id TEXT, group_id "
                        "TEXT, label TEXT, intent TEXT, keywords TEXT, facets TEXT, approved "
                        "INTEGER, proposed_by TEXT, PRIMARY KEY (dataset_id, group_id))")
            con.execute("CREATE TABLE IF NOT EXISTS keyword_runs (dataset_id TEXT PRIMARY KEY, "
                        "meta TEXT)")
            have = {r[1] for r in con.execute("PRAGMA table_info(keyword_groups)")}
            for col in ("generation", "joins"):          # B9: added to B7's table in place
                if col not in have:
                    con.execute(f"ALTER TABLE keyword_groups ADD COLUMN {col} TEXT")
            con.commit()

    # --- storage -------------------------------------------------------------------------------
    def _rows(self, did: str) -> list[dict]:
        rows = self.ds.store._q("SELECT group_id, label, intent, keywords, facets, approved, "
                                "proposed_by, generation, joins FROM keyword_groups WHERE "
                                "dataset_id=? ORDER BY approved DESC, group_id", (did,))
        return [{"group_id": r[0], "label": r[1], "intent": r[2], "keywords": json.loads(r[3]),
                 "facets": json.loads(r[4]), "approved": bool(r[5]), "proposed_by": r[6],
                 "generation": r[7], "joins": r[8]} for r in rows]

    def _save(self, did: str, g: dict) -> None:
        self.ds.store._q("INSERT OR REPLACE INTO keyword_groups (dataset_id, group_id, label, "
                         "intent, keywords, facets, approved, proposed_by, generation, joins) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (did, g["group_id"], g["label"], g["intent"],
                          json.dumps(sorted(set(g["keywords"]))), json.dumps(g["facets"]),
                          int(g["approved"]), g.get("proposed_by", "person"),
                          g.get("generation"), g.get("joins")))

    def _drop(self, did: str, gid: str) -> None:
        self.ds.store._q("DELETE FROM keyword_groups WHERE dataset_id=? AND group_id=?",
                         (did, gid))

    def _mirror(self, did: str) -> int:
        """Approved groups -> the workspace table the engine reads. Returns rows written."""
        d = self.ds._get(did)
        approved = [g for g in self._rows(did) if g["approved"]]
        with self.ds.be._workspace(d["workspace_id"]):
            con = db.connect(d["workspace_id"])
            try:
                con.execute(f"CREATE TABLE IF NOT EXISTS {GROUP_MAP} (dataset VARCHAR, "
                            f"keyword VARCHAR, group_label VARCHAR, intent VARCHAR)")
                con.execute(f"DELETE FROM {GROUP_MAP} WHERE dataset = ?", [d["name"]])
                rows = [(d["name"], k.lower().strip(), g["label"], g["intent"])
                        for g in approved for k in g["keywords"]]
                if rows:
                    con.executemany(f"INSERT INTO {GROUP_MAP} VALUES (?, ?, ?, ?)", rows)
            finally:
                con.close()
        return len(rows)

    # --- run -----------------------------------------------------------------------------------
    def run(self, did: str, column: str | None = None, backend: str = "auto") -> dict:
        if backend not in ("auto", "chargram", "ollama"):
            raise ServiceError(422, "bad_backend", "backend is auto, chargram or ollama")
        from backend.text.pipeline import group_keywords
        d = self.ds._get(did)
        if column is None:
            bound, _ = self.ds._bound(d)
            cols = [c for concept in TEXT_CONCEPTS for c in bound.get(concept, [])]
            if not cols:
                raise ServiceError(422, "needs_data", "no search term, query or keyword column")
            column = cols[0]
        con = db.connect_read_only(d["workspace_id"])
        try:
            vals = [r[0] for r in con.execute(
                f'SELECT DISTINCT CAST("{column.replace(chr(34), "")}" AS VARCHAR) FROM '
                f'"{d["name"]}" WHERE "{column.replace(chr(34), "")}" IS NOT NULL').fetchall()]
        finally:
            con.close()
        res = group_keywords(vals, backend=backend, labeler=self.labeler)
        gen = res["embedding_version"]["generation"]
        # a refresh replaces proposals; approved groups stay as the person left them
        approved = [g for g in self._rows(did) if g["approved"]]
        kept = {k for g in approved for k in g["keywords"]}
        for g in self._rows(did):
            if not g["approved"]:
                self._drop(did, g["group_id"])
        joins, carry = self._carry_forward(did, vals, kept, approved, backend, gen)
        n = 0
        for g in res["groups"]:
            kws = [k for k in g.keywords if k not in kept and k not in joins]
            if kws:
                n += 1
                self._save(did, {"group_id": f"p{secrets.token_hex(3)}", "label": g.label,
                                 "intent": g.intent, "keywords": kws, "facets": g.facets,
                                 "approved": False, "proposed_by": g.proposed_by,
                                 "generation": gen})
        by_target: dict[str, list[str]] = {}
        for k, (gid, _) in joins.items():
            by_target.setdefault(gid, []).append(k)
        target = {g["group_id"]: g for g in approved}
        for gid, kws in by_target.items():
            n += 1
            self._save(did, {"group_id": f"p{secrets.token_hex(3)}",
                             "label": f"{target[gid]['label']} (new keywords)",
                             "intent": target[gid]["intent"], "keywords": kws,
                             "facets": target[gid]["facets"], "approved": False,
                             "proposed_by": "rules", "generation": gen, "joins": gid})
        meta = {"column": column, "embedding": res["embedding"], "threshold": res["threshold"],
                "embedding_version": res["embedding_version"],
                "typos_merged": res["typos_merged"], "keywords": res["input"],
                "proposed_groups": n, "carry_forward": carry}
        self.ds.store._q("INSERT OR REPLACE INTO keyword_runs VALUES (?, ?)",
                         (did, json.dumps(meta)))
        return self.list(did)

    def _carry_forward(self, did, vals, kept, approved, backend, gen) -> tuple[dict, dict]:
        """New keywords proposed to join approved groups -- only within one embedding
        generation (B9 concepts 1 and 16); a switch is said, never silently bridged."""
        from backend.text.pipeline import carry_forward
        new = [v for v in vals if v not in kept]
        if not approved or not new:
            return {}, {"status": "nothing to carry", "suggested": 0}
        gens = {g["generation"] for g in approved}
        if gens != {gen}:
            return {}, {"status": "disabled", "suggested": 0, "reason": (
                f"approved groups were built with embedding generation(s) "
                f"{sorted(x or 'unrecorded' for x in gens)}; this run used {gen}. Vectors of "
                f"different generations are not compared: re-run with the backend the groups "
                f"were approved under, or re-approve under this one.")}
        joins, _ = carry_forward(new, {g["group_id"]: g["keywords"] for g in approved},
                                 backend=backend)
        return joins, {"status": "ok", "suggested": len(joins),
                       "note": "proposals to join approved groups: approve by merging "
                               "[approved group, this proposal] (the approved group's id "
                               "first keeps it approved)"}

    def list(self, did: str) -> dict:
        self.ds._get(did)
        meta = self.ds.store._q("SELECT meta FROM keyword_runs WHERE dataset_id=?", (did,))
        return {"dataset_id": did, "groups": self._rows(did),
                "run": json.loads(meta[0][0]) if meta else None}

    # --- the person's actions ------------------------------------------------------------------
    def act(self, did: str, a: dict) -> dict:
        groups = {g["group_id"]: g for g in self._rows(did)}
        ids = a.get("group_ids") or []
        missing = [i for i in ids if i not in groups]
        if missing or not ids:
            raise ServiceError(404 if missing else 422, "not_found" if missing else "bad_action",
                               f"unknown group(s): {missing}" if missing else "group_ids needed")
        kind = a["action"]
        if kind == "approve":
            for i in ids:
                groups[i]["approved"] = True
                self._save(did, groups[i])
        elif kind == "rename":
            if not a.get("label"):
                raise ServiceError(422, "bad_action", "rename needs label")
            g = groups[ids[0]]
            g.update(label=a["label"], proposed_by="person")
            self._save(did, g)
        elif kind == "merge":
            if len(ids) < 2:
                raise ServiceError(422, "bad_action", "merge needs two or more group_ids")
            into = groups[ids[0]]
            for i in ids[1:]:
                into["keywords"] += groups[i]["keywords"]
                for f, v in groups[i]["facets"].items():
                    into["facets"][f] = sorted(set(into["facets"].get(f, [])) | set(v))
                self._drop(did, i)
            if a.get("label"):
                into["label"] = a["label"]
            into["proposed_by"] = "person"
            self._save(did, into)
        elif kind == "move_keyword":
            kw, tgt = a.get("keyword"), a.get("target_group_id")
            src = groups[ids[0]]
            if kw not in src["keywords"] or tgt not in groups:
                raise ServiceError(422, "bad_action", "keyword not in group, or no target group")
            src["keywords"].remove(kw)
            groups[tgt]["keywords"].append(kw)
            self._save(did, groups[tgt])
            self._save(did, src) if src["keywords"] else self._drop(did, src["group_id"])
        elif kind == "split":
            kws = a.get("keywords") or ([a["keyword"]] if a.get("keyword") else [])
            src = groups[ids[0]]
            if not kws or not set(kws) <= set(src["keywords"]) or set(kws) == set(src["keywords"]):
                raise ServiceError(422, "bad_action", "split needs some (not all) of the "
                                                      "group's keywords")
            src["keywords"] = [k for k in src["keywords"] if k not in kws]
            self._save(did, src)
            self._save(did, {"group_id": f"p{secrets.token_hex(3)}",
                             "label": a.get("label") or f"{src['label']} (split)",
                             "intent": src["intent"], "keywords": kws, "facets": src["facets"],
                             "approved": False, "proposed_by": "person"})
        else:
            raise ServiceError(422, "bad_action", f"unknown action {kind}")
        self._mirror(did)
        return self.list(did)
