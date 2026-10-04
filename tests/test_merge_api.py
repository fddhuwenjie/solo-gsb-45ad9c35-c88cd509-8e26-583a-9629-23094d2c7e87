"""End-to-end tests for the POST /fixtures/merge API."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ------------------------------------------------------------- fixtures


def users_orders_schema():
    return [
        {"name": "users", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "email": {"type": "email", "unique": True},
            "name": {"type": "name"}}},
        {"name": "orders", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100000,
                   "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]


def fx(users, orders, seed=1):
    return {
        "seed": seed,
        "row_counts": {"users": len(users), "orders": len(orders)},
        "tables": {"users": users, "orders": orders},
    }


def merge(fixtures, tables, seed=42):
    return client.post("/fixtures/merge", json={
        "fixtures": fixtures, "tables": tables, "seed": seed})


def generate(tables, seed=42):
    r = client.post("/fixtures", json={"tables": tables, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------- basics


def test_merge_identical_rows_are_deduplicated():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1}],
    )
    fx2 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},   # identical copy
         {"id": 3, "email": "c@x.example", "name": "E F"}],
        [{"id": 10, "total": 1.5, "user_id": 1}],            # identical copy
        seed=2,
    )
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["row_counts"] == {"users": 3, "orders": 1}
    assert data["tables"]["users"] == (
        fx1["tables"]["users"] + [fx2["tables"]["users"][1]]
    )
    assert data["tables"]["orders"] == fx1["tables"]["orders"]


def test_merge_merging_fixture_with_itself_is_a_noop():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1},
         {"id": 11, "total": 2.5, "user_id": 2}],
    )
    r = merge([fx1, fx1], tables)
    assert r.status_code == 200, r.text
    assert r.json()["tables"] == fx1["tables"]
    assert r.json()["row_counts"] == {"users": 2, "orders": 2}


def test_merge_disjoint_fixtures_are_concatenated_verbatim():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tables"]["users"] == (
        fx1["tables"]["users"] + fx2["tables"]["users"]
    )
    assert data["tables"]["orders"] == (
        fx1["tables"]["orders"] + fx2["tables"]["orders"]
    )


def test_merge_conflicting_unique_key_rewrites_and_cascades():
    tables = users_orders_schema()
    fx1 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},
         {"id": 2, "email": "b@x.example", "name": "C D"}],
        [{"id": 10, "total": 1.5, "user_id": 1},
         {"id": 11, "total": 2.5, "user_id": 2}],
    )
    fx2 = fx(
        [{"id": 1, "email": "a@x.example", "name": "A B"},   # deduplicated
         {"id": 2, "email": "c@x.example", "name": "E F"}],  # id clash
        [{"id": 10, "total": 9.5, "user_id": 1},             # id clash
         {"id": 12, "total": 3.5, "user_id": 2}],            # follows its user
        seed=2,
    )
    r = merge([fx1, fx2], tables, seed=7)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 7
    assert data["row_counts"] == {"users": 3, "orders": 4}

    users = data["tables"]["users"]
    # row order is stable: fixture order, then original row order
    assert [u["email"] for u in users] == [
        "a@x.example", "b@x.example", "c@x.example"]
    new_id = users[2]["id"]
    assert isinstance(new_id, int) and 1 <= new_id <= 100000
    assert new_id not in (1, 2)
    # non-conflicting values of the rewritten row are preserved verbatim
    assert users[2]["name"] == "E F"

    orders = data["tables"]["orders"]
    assert [o["total"] for o in orders] == [1.5, 2.5, 9.5, 3.5]
    assert orders[2]["id"] not in (10, 11, 12)
    assert orders[2]["user_id"] == 1        # still points at the deduplicated user
    assert orders[3]["id"] == 12            # no clash: untouched
    assert orders[3]["user_id"] == new_id   # cascade followed the rewrite
    user_ids = {u["id"] for u in users}
    assert all(o["user_id"] in user_ids for o in orders)


def test_merge_extra_fields_and_untouched_rows_are_verbatim():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B",
               "extra": {"keep": [1, 2]}}],
             [{"id": 10, "total": 1.5, "user_id": 1, "note": "x"}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D",
               "extra": "also-kept"}],
             [{"id": 11, "total": 2.5, "user_id": 1}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    assert out["users"][0] == fx1["tables"]["users"][0]      # untouched row
    assert out["users"][1]["extra"] == "also-kept"           # extra field kept
    assert out["users"][1]["id"] != 1                        # but id rewritten
    # fx1's order is verbatim; fx2's order followed its own user's rewrite
    assert out["orders"][0] == fx1["tables"]["orders"][0]
    assert out["orders"][1]["user_id"] == out["users"][1]["id"]
    assert out["orders"][1]["total"] == 2.5


def test_merge_table_order_follows_schema_declaration():
    tables = list(reversed(users_orders_schema()))  # child declared first
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    assert list(r.json()["tables"]) == ["orders", "users"]


def test_merge_null_unique_values_never_collide():
    tables = [{"name": "t", "rows": 2, "fields": {
        "x": {"type": "int", "unique": True, "nullable": True}}}]
    fx1 = {"seed": 1, "row_counts": {"t": 2}, "tables": {"t": [
        {"x": None, "tag": "a"}, {"x": 1, "tag": "b"}]}}
    fx2 = {"seed": 2, "row_counts": {"t": 2}, "tables": {"t": [
        {"x": None, "tag": "a"}, {"x": 1, "tag": "c"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["t"]
    # identical rows whose only unique key is NULL are both kept (SQL semantics)
    assert rows[0] == {"x": None, "tag": "a"}
    assert rows[2] == {"x": None, "tag": "a"}
    # the non-null clash is rewritten
    assert rows[1]["x"] == 1
    assert rows[3]["x"] is not None and rows[3]["x"] != 1
    assert rows[3]["tag"] == "c"


def test_merge_constraint_free_table_keeps_duplicate_rows():
    tables = [{"name": "logs", "rows": 1,
               "fields": {"msg": {"type": "string"}}}]
    fx1 = {"seed": 1, "row_counts": {"logs": 1},
           "tables": {"logs": [{"msg": "same"}]}}
    fx2 = {"seed": 2, "row_counts": {"logs": 1},
           "tables": {"logs": [{"msg": "same"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    # no unique key -> nothing can conflict -> both rows stay
    assert r.json()["tables"]["logs"] == [{"msg": "same"}, {"msg": "same"}]


# ------------------------------------------------------- composite unique


def test_merge_composite_unique_conflict_is_rewritten():
    tables = [{"name": "slots", "rows": 2, "fields": {
        "day": {"type": "enum", "choices": ["mon", "tue"]},
        "hour": {"type": "enum", "choices": ["10", "11"]},
        "note": {"type": "string"}},
        "unique_constraints": [["day", "hour"]]}]
    fx1 = {"seed": 1, "row_counts": {"slots": 2}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "aaaaaaaa"},
        {"day": "mon", "hour": "11", "note": "bbbbbbbb"},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"slots": 2}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "cccccccc"},  # tuple clash
        {"day": "tue", "hour": "10", "note": "dddddddd"},
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["slots"]
    assert len(rows) == 4
    keys = [(x["day"], x["hour"]) for x in rows]
    assert len(set(keys)) == 4
    # the first occurrence is kept verbatim; the clash moved to the only
    # free combination (tue, 11) and kept its other fields
    assert rows[0] == {"day": "mon", "hour": "10", "note": "aaaaaaaa"}
    assert rows[2]["note"] == "cccccccc"
    assert (rows[2]["day"], rows[2]["hour"]) == ("tue", "11")
    assert rows[3] == {"day": "tue", "hour": "10", "note": "dddddddd"}


def test_merge_composite_unique_space_exhausted_is_422():
    tables = [{"name": "slots", "rows": 3, "fields": {
        "day": {"type": "enum", "choices": ["mon", "tue"]},
        "hour": {"type": "enum", "choices": ["10", "11"]},
        "note": {"type": "string"}},
        "unique_constraints": [["day", "hour"]]}]
    fx1 = {"seed": 1, "row_counts": {"slots": 3}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "aaaaaaaa"},
        {"day": "mon", "hour": "11", "note": "bbbbbbbb"},
        {"day": "tue", "hour": "10", "note": "cccccccc"},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"slots": 3}, "tables": {"slots": [
        {"day": "mon", "hour": "10", "note": "dddddddd"},
        {"day": "mon", "hour": "11", "note": "eeeeeeee"},
        {"day": "tue", "hour": "11", "note": "ffffffff"},
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


# ----------------------------------------------------------- foreign keys


def test_merge_unique_fk_column_repicks_from_parent_pool():
    tables = [
        {"name": "users", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "orders", "rows": 1, "fields": {
            "user_id": {"type": "int", "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"users": 2, "orders": 1}, "tables": {
        "users": [{"id": 1}, {"id": 2}],
        "orders": [{"user_id": 1, "total": 1.5}],
    }}
    fx2 = {"seed": 2, "row_counts": {"users": 2, "orders": 1}, "tables": {
        "users": [{"id": 1}, {"id": 2}],  # identical: deduplicated
        "orders": [{"user_id": 1, "total": 2.5}],  # unique FK clash
    }}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    orders = r.json()["tables"]["orders"]
    assert [o["total"] for o in orders] == [1.5, 2.5]
    # the clashing FK value is repicked from the existing parent values
    assert sorted(o["user_id"] for o in orders) == [1, 2]


def test_merge_unique_fk_pool_exhausted_is_422():
    tables = [
        {"name": "users", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 100,
                   "unique": True}}},
        {"name": "orders", "rows": 1, "fields": {
            "user_id": {"type": "int", "unique": True},
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"users": 1, "orders": 1}, "tables": {
        "users": [{"id": 1}], "orders": [{"user_id": 1, "total": 1.5}]}}
    fx2 = {"seed": 2, "row_counts": {"users": 1, "orders": 1}, "tables": {
        "users": [{"id": 1}], "orders": [{"user_id": 1, "total": 2.5}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


def test_merge_cascade_flows_through_multi_level_chain():
    tables = [
        {"name": "companies", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "name": {"type": "string"}}},
        {"name": "departments", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "company_id", "references_table": "companies",
              "nullable": False}]},
        {"name": "employees", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "dept_id", "references_table": "departments",
              "nullable": False}]},
    ]

    def chain_fx(company_name, seed):
        return {"seed": seed,
                "row_counts": {"companies": 1, "departments": 1,
                               "employees": 1},
                "tables": {
                    "companies": [{"id": 1, "name": company_name}],
                    "departments": [{"id": 10, "company_id": 1}],
                    "employees": [{"id": 100, "dept_id": 10}],
                }}

    r = merge([chain_fx("aaaaaaaa", 1), chain_fx("bbbbbbbb", 2)], tables)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    # every level clashed and was rewritten; references followed along
    assert out["companies"][0] == {"id": 1, "name": "aaaaaaaa"}
    new_company = out["companies"][1]
    assert new_company["id"] != 1 and new_company["name"] == "bbbbbbbb"
    new_dept = out["departments"][1]
    assert new_dept["id"] != 10
    assert new_dept["company_id"] == new_company["id"]
    new_emp = out["employees"][1]
    assert new_emp["id"] != 100
    assert new_emp["dept_id"] == new_dept["id"]
    # first fixture's rows are untouched
    assert out["departments"][0] == {"id": 10, "company_id": 1}
    assert out["employees"][0] == {"id": 100, "dept_id": 10}


def test_merge_three_level_carried_fk_follows_own_source_chain():
    """A→B→C where B's *own* unique key is the FK to A, and C references
    that same column. When the second fixture's A key collides (same key,
    different content), A is rewritten, B's key follows — and C must follow
    B's new key too, not stay on the first fixture's B.
    """
    tables = [
        {"name": "accounts", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "name": {"type": "string"}}},
        {"name": "members", "rows": 1, "fields": {
            "account_id": {"type": "int", "min_value": 1, "max_value": 1000,
                           "unique": True},
            "tier": {"type": "string"}},
         "foreign_keys": [
             {"column": "account_id", "references_table": "accounts",
              "nullable": False}]},
        {"name": "events", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "member_account", "references_table": "members",
              "references_column": "account_id", "nullable": False}]},
    ]
    fx1 = {"seed": 1, "row_counts": {"accounts": 1, "members": 1, "events": 2},
           "tables": {
               "accounts": [{"id": 1, "name": "one11111"}],
               "members": [{"account_id": 1, "tier": "gold0000"}],
               # two children, both carrying an extra field verbatim
               "events": [
                   {"id": 100, "member_account": 1, "kind": "login000"},
                   {"id": 101, "member_account": 1, "kind": "click000"}],
           }}
    fx2 = {"seed": 2, "row_counts": {"accounts": 1, "members": 1, "events": 2},
           "tables": {
               "accounts": [{"id": 1, "name": "two22222"}],  # key clash
               "members": [{"account_id": 1, "tier": "silver00"}],
               "events": [
                   {"id": 200, "member_account": 1, "kind": "login000"},
                   {"id": 201, "member_account": 1, "kind": "view0000"}],
           }}
    r = merge([fx1, fx2], tables, seed=42)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]

    accounts = out["accounts"]
    assert accounts[0] == {"id": 1, "name": "one11111"}
    new_account_id = accounts[1]["id"]
    assert new_account_id != 1 and accounts[1]["name"] == "two22222"

    members = out["members"]
    # fx1's member is untouched; fx2's member followed its own account
    assert members[0] == {"account_id": 1, "tier": "gold0000"}
    assert members[1] == {"account_id": new_account_id, "tier": "silver00"}

    events = out["events"]
    assert [e["id"] for e in events] == [100, 101, 200, 201]
    # every reference from fx1 lands on fx1's member; every reference from
    # fx2 lands on fx2's member — never cross-linked to the other source
    assert {e["member_account"] for e in events[:2]} == {1}
    assert {e["member_account"] for e in events[2:]} == {new_account_id}
    # unconflicted values and extra fields are verbatim
    assert events[2]["kind"] == "login000"
    assert events[3]["kind"] == "view0000"
    member_keys = {m["account_id"] for m in members}
    assert all(e["member_account"] in member_keys for e in events)


def test_merge_four_level_carried_fk_chain_tracks_to_final_key():
    """The lineage tracking extends over an arbitrary chain length: a value
    moved at level 1 keeps propagating through levels 2, 3 and 4 from each
    child's original reference.
    """
    tables = [
        {"name": "a", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "label": {"type": "string"}}},
        {"name": "b", "rows": 1, "fields": {
            "a_id": {"type": "int", "min_value": 1, "max_value": 1000,
                     "unique": True}},
         "foreign_keys": [
             {"column": "a_id", "references_table": "a", "nullable": False}]},
        {"name": "c", "rows": 1, "fields": {
            "b_aid": {"type": "int", "min_value": 1, "max_value": 1000,
                      "unique": True}},
         "foreign_keys": [
             {"column": "b_aid", "references_table": "b",
              "references_column": "a_id", "nullable": False}]},
        {"name": "d", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "c_baid", "references_table": "c",
              "references_column": "b_aid", "nullable": False}]},
    ]

    def chain_fx(label, seed):
        return {"seed": seed,
                "row_counts": {"a": 1, "b": 1, "c": 1, "d": 1},
                "tables": {
                    "a": [{"id": 1, "label": label}],
                    "b": [{"a_id": 1}],
                    "c": [{"b_aid": 1}],
                    "d": [{"id": 900, "c_baid": 1}],
                }}

    r = merge([chain_fx("one11111", 1), chain_fx("two22222", 2)],
              tables, seed=11)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]
    new_a = out["a"][1]["id"]
    assert new_a != 1
    assert out["b"][1]["a_id"] == new_a
    assert out["c"][1]["b_aid"] == new_a
    assert out["d"][1]["c_baid"] == new_a
    # first fixture's whole chain is verbatim
    assert out["a"][0] == {"id": 1, "label": "one11111"}
    assert out["b"][0] == {"a_id": 1}
    assert out["c"][0] == {"b_aid": 1}
    assert out["d"][0] == {"id": 900, "c_baid": 1}


def test_merge_consecutive_rewrites_dedup_then_repick_points_at_kept_row():
    """A copied B row is deduplicated against an earlier fixture, and the
    surviving copy must itself be repicked to another parent (unique FK
    clash). A third fixture whose references used the old value must end on
    the record that actually survives — after every consecutive rewrite.
    """
    tables = [
        {"name": "a", "rows": 2, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}}},
        {"name": "b", "rows": 1, "fields": {
            "a_id": {"type": "int", "min_value": 1, "max_value": 1000,
                     "unique": True},
            "tag": {"type": "string"}},
         "foreign_keys": [
             {"column": "a_id", "references_table": "a", "nullable": False}]},
        {"name": "c", "rows": 1, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True}},
         "foreign_keys": [
             {"column": "b_aid", "references_table": "b",
              "references_column": "a_id", "nullable": False}]},
    ]

    def fixture(seed, a_rows, b_row, c_id):
        return {"seed": seed,
                "row_counts": {"a": len(a_rows), "b": 1, "c": 1},
                "tables": {
                    "a": [{"id": v} for v in a_rows],
                    "b": [b_row],
                    "c": [{"id": c_id, "b_aid": b_row["a_id"]}],
                }}

    fx1 = fixture(1, [1, 2], {"a_id": 1, "tag": "kept0000"}, 100)
    # identical B copy from fx2 is the first duplicate -> kept as second B
    fx2 = fixture(2, [1], {"a_id": 1, "tag": "copied00"}, 200)
    # same identical B copy again -> deduplicated against fx2's survivor,
    # which then has to repick a_id (1 is already taken by fx1's B)
    fx3 = fixture(3, [1], {"a_id": 1, "tag": "copied00"}, 300)
    r = merge([fx1, fx2, fx3], tables, seed=42)
    assert r.status_code == 200, r.text
    out = r.json()["tables"]

    members = out["b"]
    assert len(members) == 2
    assert members[0] == {"a_id": 1, "tag": "kept0000"}
    kept_aid = members[1]["a_id"]
    assert kept_aid == 2  # only other parent value available
    assert members[1]["tag"] == "copied00"

    events = out["c"]
    assert [e["id"] for e in events] == [100, 200, 300]
    # fx2's and fx3's references both end on the single surviving copied B,
    # even though fx3's copy was dropped and the kept one was repicked
    assert events[0]["b_aid"] == 1
    assert events[1]["b_aid"] == kept_aid
    assert events[2]["b_aid"] == kept_aid


def test_merge_self_referencing_tree_cascades():
    tables = [{"name": "nodes", "rows": 2, "fields": {
        "id": {"type": "int", "min_value": 1, "max_value": 1000,
               "unique": True}},
        "foreign_keys": [
            {"column": "parent_id", "references_table": "nodes",
             "references_column": "id", "nullable": True}]}]
    fx1 = {"seed": 1, "row_counts": {"nodes": 1}, "tables": {"nodes": [
        {"id": 1, "parent_id": None},
    ]}}
    fx2 = {"seed": 2, "row_counts": {"nodes": 2}, "tables": {"nodes": [
        {"id": 1, "parent_id": None, "tag": "x"},   # id clash -> rewritten
        {"id": 5, "parent_id": 1},                  # follows the rewrite
    ]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["nodes"]
    assert len(rows) == 3
    assert rows[0] == {"id": 1, "parent_id": None}
    new_root = rows[1]
    assert new_root["id"] != 1 and new_root["parent_id"] is None
    assert new_root["tag"] == "x"                  # extra field preserved
    assert rows[2]["id"] == 5
    assert rows[2]["parent_id"] == new_root["id"]  # self-reference cascaded


# ------------------------------------------------------------- determinism


def test_merge_same_seed_reproducible_different_seed_may_differ():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D"}],
             [{"id": 10, "total": 2.5, "user_id": 1}], seed=2)
    r1 = merge([fx1, fx2], tables, seed=55)
    r2 = merge([fx1, fx2], tables, seed=55)
    assert r1.status_code == 200 and r1.json() == r2.json()
    assert any(
        merge([fx1, fx2], tables, seed=s).json() != r1.json()
        for s in range(56, 66)
    )


def test_merge_output_can_be_merged_again():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 1, "email": "b@x.example", "name": "C D"}],
             [{"id": 10, "total": 2.5, "user_id": 1}], seed=2)
    fx3 = fx([{"id": 2, "email": "c@x.example", "name": "E F"}],
             [{"id": 11, "total": 3.5, "user_id": 2}], seed=3)
    first = merge([fx1, fx2], tables, seed=5)
    assert first.status_code == 200, first.text
    second = merge([first.json(), fx3], tables, seed=6)
    assert second.status_code == 200, second.text
    data = second.json()
    users = data["tables"]["users"]
    assert len(users) == 3
    assert len({u["id"] for u in users}) == 3
    assert len({u["email"] for u in users}) == 3
    ids = {u["id"] for u in users}
    assert all(o["user_id"] in ids for o in data["tables"]["orders"])


def test_merge_two_generated_fixtures_end_to_end():
    tables = [
        {"name": "users", "rows": 5, "fields": {
            "id": {"type": "int", "min_value": 1, "max_value": 1000,
                   "unique": True},
            "email": {"type": "email", "unique": True}}},
        {"name": "orders", "rows": 10, "fields": {
            "total": {"type": "float"}},
         "foreign_keys": [
             {"column": "user_id", "references_table": "users",
              "nullable": False}]},
    ]
    fx1 = generate(tables, seed=3)
    fx2 = generate(tables, seed=4)
    r = merge([fx1, fx2], tables, seed=9)
    assert r.status_code == 200, r.text
    data = r.json()
    users = data["tables"]["users"]
    orders = data["tables"]["orders"]
    assert len(users) == 10 and len(orders) == 20
    assert len({u["id"] for u in users}) == 10
    assert len({u["email"] for u in users}) == 10
    ids = {u["id"] for u in users}
    assert all(o["user_id"] in ids for o in orders)


# ------------------------------------------------------------- 422 paths


def test_merge_single_fixture_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}], [])
    fx1["row_counts"] = {"users": 1, "orders": 0}
    assert merge([fx1], tables).status_code == 422
    assert merge([], tables).status_code == 422


def test_merge_fixture_table_set_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = {"seed": 2, "row_counts": {"users": 1},
           "tables": {"users": [
               {"id": 2, "email": "b@x.example", "name": "C D"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["type"] == "fixture_shape_mismatch"
    assert detail["loc"][:3] == ["body", "fixtures", 1]
    assert set(r.json()) == {"detail"}


def test_merge_row_counts_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    fx2["row_counts"]["users"] = 99
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    detail = r.json()["detail"][0]
    assert detail["type"] == "fixture_shape_mismatch"
    assert detail["loc"][:3] == ["body", "fixtures", 1]


def test_merge_value_type_mismatch_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "not-an-email", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 2}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unsupported_type"


def test_merge_raw_unique_violation_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 5, "email": "b@x.example", "name": "C D"},
              {"id": 5, "email": "e@x.example", "name": "E F"}],
             [{"id": 11, "total": 2.5, "user_id": 5}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_unique_violation"


def test_merge_raw_fk_violation_is_422():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    fx2 = fx([{"id": 2, "email": "b@x.example", "name": "C D"}],
             [{"id": 11, "total": 2.5, "user_id": 999}], seed=2)
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fixture_fk_violation"


def test_merge_unique_space_exhausted_is_422():
    tables = [{"name": "flags", "rows": 2, "fields": {
        "b": {"type": "boolean", "unique": True},
        "tag": {"type": "string"}}}]
    fx1 = {"seed": 1, "row_counts": {"flags": 2}, "tables": {"flags": [
        {"b": True, "tag": "aaaaaaaa"}, {"b": False, "tag": "bbbbbbbb"}]}}
    fx2 = {"seed": 2, "row_counts": {"flags": 2}, "tables": {"flags": [
        {"b": True, "tag": "cccccccc"}, {"b": False, "tag": "dddddddd"}]}}
    r = merge([fx1, fx2], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert set(r.json()) == {"detail"}


def test_merge_schema_cycle_is_422():
    tables = [
        {"name": "a", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "b_id", "references_table": "b",
                           "references_column": "id"}]},
        {"name": "b", "rows": 1, "fields": {"id": {"type": "int", "unique": True}},
         "foreign_keys": [{"column": "a_id", "references_table": "a",
                           "references_column": "id"}]},
    ]
    empty = {"seed": 1, "row_counts": {"a": 0, "b": 0},
             "tables": {"a": [], "b": []}}
    r = merge([empty, empty], tables)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"
    assert set(r.json()) == {"detail"}


def test_merge_missing_required_body_fields_is_422():
    assert client.post("/fixtures/merge", json={}).status_code == 422
    r = client.post("/fixtures/merge", json={"tables": users_orders_schema()})
    assert r.status_code == 422  # fixtures is required


def test_merge_extra_request_fields_forbidden():
    tables = users_orders_schema()
    fx1 = fx([{"id": 1, "email": "a@x.example", "name": "A B"}],
             [{"id": 10, "total": 1.5, "user_id": 1}])
    r = client.post("/fixtures/merge", json={
        "fixtures": [fx1, fx1], "tables": tables, "locale": "zh"})
    assert r.status_code == 422
