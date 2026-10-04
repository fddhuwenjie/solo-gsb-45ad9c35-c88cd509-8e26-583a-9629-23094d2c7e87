"""End-to-end tests for the /fixtures API."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def id_table(name="users", rows=5, col="id"):
    return {
        "name": name,
        "rows": rows,
        "fields": {
            col: {"type": "int", "min_value": 1, "max_value": 100000, "unique": True},
            "name": {"type": "name"},
        },
    }


# ---------------------------------------------------------------- basics


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_basic_generation_shape():
    body = {"tables": [id_table(rows=4)], "seed": 7}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 7
    assert data["row_counts"] == {"users": 4}
    assert len(data["tables"]["users"]) == 4
    for row in data["tables"]["users"]:
        assert set(row) == {"id", "name"}
        assert isinstance(row["id"], int)
        assert isinstance(row["name"], str)


def test_empty_table():
    body = {"tables": [id_table(rows=0)], "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200
    assert r.json()["tables"]["users"] == []
    assert r.json()["row_counts"]["users"] == 0


def test_response_table_order_follows_request():
    body = {
        "tables": [
            id_table(name="zebra", rows=2),
            id_table(name="alpha", rows=2),
        ],
        "seed": 3,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200
    assert list(r.json()["tables"]) == ["zebra", "alpha"]


# ------------------------------------------------------------- uniqueness


def test_single_column_unique():
    body = {"tables": [id_table(rows=20)], "seed": 11}
    rows = client.post("/fixtures", json=body).json()["tables"]["users"]
    assert len({r["id"] for r in rows}) == 20


def test_composite_unique_constraint():
    body = {
        "tables": [
            {
                "name": "slots",
                "rows": 12,
                "fields": {
                    "day": {"type": "enum", "choices": ["mon", "tue", "wed"],
                            "nullable": False},
                    "hour": {"type": "enum", "choices": ["10", "11", "12", "13"],
                             "nullable": False},
                },
                "unique_constraints": [["day", "hour"]],
            }
        ],
        "seed": 5,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["slots"]
    pairs = {(r["day"], r["hour"]) for r in rows}
    assert len(pairs) == 12


def test_unique_space_too_small_is_422():
    body = {
        "tables": [
            {
                "name": "flags",
                "rows": 3,
                "fields": {"b": {"type": "boolean", "unique": True}},
            }
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"
    assert "tables" not in r.json()  # never leak partial data


def test_composite_unique_space_too_small_is_422():
    body = {
        "tables": [
            {
                "name": "slots",
                "rows": 5,
                "fields": {
                    "day": {"type": "enum", "choices": ["a", "b"]},
                    "hour": {"type": "enum", "choices": ["x", "y"]},
                },
                "unique_constraints": [["day", "hour"]],
            }
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"


# ----------------------------------------------------------- foreign keys


def orders_body(seed=9, orders_rows=10, nullable=False):
    return {
        "tables": [
            id_table(name="users", rows=5),
            {
                "name": "orders",
                "rows": orders_rows,
                "fields": {"total": {"type": "float", "min_value": 0, "max_value": 99}},
                "foreign_keys": [
                    {
                        "column": "user_id",
                        "references_table": "users",
                        "references_column": "id",
                        "nullable": nullable,
                    }
                ],
            },
        ],
        "seed": seed,
    }


def test_foreign_keys_reference_existing_parent_rows():
    r = client.post("/fixtures", json=orders_body())
    assert r.status_code == 200, r.text
    data = r.json()["tables"]
    parent_ids = {u["id"] for u in data["users"]}
    for order in data["orders"]:
        assert "user_id" in order  # auto-created FK column
        assert order["user_id"] in parent_ids


def test_foreign_key_child_can_be_declared_before_parent():
    # Dependency order must be inferred even when the child is listed first.
    body = {"tables": list(reversed(orders_body()["tables"])), "seed": 9}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    parent_ids = {u["id"] for u in r.json()["tables"]["users"]}
    assert all(o["user_id"] in parent_ids for o in r.json()["tables"]["orders"])


def test_unique_fk_spreads_over_all_parents():
    body = orders_body(orders_rows=5)
    body["tables"][1]["fields"]["user_id"] = {"type": "int", "unique": True}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    ids = [o["user_id"] for o in r.json()["tables"]["orders"]]
    assert len(set(ids)) == 5  # every order maps to a distinct user


def test_unique_fk_exceeding_parent_rows_is_422():
    body = orders_body(orders_rows=6)
    body["tables"][1]["fields"]["user_id"] = {"type": "int", "unique": True}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unique_space_exhausted"


def test_empty_parent_non_nullable_fk_is_422():
    body = orders_body(orders_rows=3, nullable=False)
    body["tables"][0]["rows"] = 0
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "empty_fk_target"
    assert set(r.json()) == {"detail"}


def test_empty_parent_nullable_fk_yields_nulls():
    body = orders_body(orders_rows=3, nullable=True)
    body["tables"][0]["rows"] = 0
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200
    assert all(o["user_id"] is None for o in r.json()["tables"]["orders"])


def test_three_table_chain_and_diamond():
    body = {
        "tables": [
            id_table(name="companies", rows=3),
            {
                "name": "departments",
                "rows": 6,
                "fields": {
                    "dept_no": {"type": "int", "min_value": 1, "max_value": 100000,
                                "unique": True},
                    "title": {"type": "string"},
                },
                "foreign_keys": [
                    {"column": "company_id", "references_table": "companies",
                     "nullable": False}
                ],
            },
            {
                "name": "employees",
                "rows": 12,
                "fields": {"email": {"type": "email", "unique": True}},
                "foreign_keys": [
                    {"column": "company_id", "references_table": "companies",
                     "nullable": False},
                    {"column": "dept_no", "references_table": "departments",
                     "references_column": "dept_no", "nullable": True},
                ],
            },
        ],
        "seed": 21,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    data = r.json()["tables"]
    company_ids = {c["id"] for c in data["companies"]}
    dept_nos = {d["dept_no"] for d in data["departments"]}
    assert all(d["company_id"] in company_ids for d in data["departments"])
    for e in data["employees"]:
        assert e["company_id"] in company_ids
        assert e["dept_no"] is None or e["dept_no"] in dept_nos
    emails = [e["email"] for e in data["employees"]]
    assert len(set(emails)) == 12


# -------------------------------------------------------------- self ref


def test_self_referencing_fk_first_row_null():
    body = {
        "tables": [
            {
                "name": "nodes",
                "rows": 8,
                "fields": {
                    "id": {"type": "int", "min_value": 1, "max_value": 1000,
                           "unique": True},
                },
                "foreign_keys": [
                    {"column": "parent_id", "references_table": "nodes",
                     "references_column": "id", "nullable": True}
                ],
            }
        ],
        "seed": 4,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    rows = r.json()["tables"]["nodes"]
    assert rows[0]["parent_id"] is None
    earlier_ids: set = set()
    for row in rows:
        if row["parent_id"] is not None:
            assert row["parent_id"] in earlier_ids
        earlier_ids.add(row["id"])


def test_self_referencing_non_nullable_is_422():
    body = {
        "tables": [
            {
                "name": "nodes",
                "rows": 3,
                "fields": {"id": {"type": "int", "unique": True}},
                "foreign_keys": [
                    {"column": "parent_id", "references_table": "nodes",
                     "references_column": "id", "nullable": False}
                ],
            }
        ],
        "seed": 4,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "empty_fk_target"


def test_self_reference_target_must_be_unique():
    body = {
        "tables": [
            {
                "name": "nodes",
                "rows": 3,
                "fields": {"label": {"type": "string"}},
                "foreign_keys": [
                    {"column": "parent", "references_table": "nodes",
                     "references_column": "label"}
                ],
            }
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fk_target_not_unique"


# ------------------------------------------------------------- validation


def test_unknown_fk_table_is_422():
    body = {
        "tables": [
            {
                "name": "orders",
                "rows": 2,
                "fields": {"total": {"type": "float"}},
                "foreign_keys": [
                    {"column": "user_id", "references_table": "ghosts"}
                ],
            }
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_fk_table"


def test_unknown_fk_column_is_422():
    body = {
        "tables": [
            id_table(name="users", rows=3),
            {
                "name": "orders",
                "rows": 2,
                "fields": {"total": {"type": "float"}},
                "foreign_keys": [
                    {"column": "uid", "references_table": "users",
                     "references_column": "missing"}
                ],
            },
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_fk_column"


def test_fk_target_must_be_unique():
    body = {
        "tables": [
            {
                "name": "users",
                "rows": 3,
                "fields": {"name": {"type": "name"}},  # not unique
            },
            {
                "name": "orders",
                "rows": 2,
                "fields": {"total": {"type": "float"}},
                "foreign_keys": [
                    {"column": "uid", "references_table": "users",
                     "references_column": "name"}
                ],
            },
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "fk_target_not_unique"


def test_dependency_cycle_is_422():
    body = {
        "tables": [
            {
                "name": "a",
                "rows": 2,
                "fields": {"id": {"type": "int", "unique": True}},
                "foreign_keys": [
                    {"column": "b_id", "references_table": "b",
                     "references_column": "id"}
                ],
            },
            {
                "name": "b",
                "rows": 2,
                "fields": {"id": {"type": "int", "unique": True}},
                "foreign_keys": [
                    {"column": "a_id", "references_table": "a",
                     "references_column": "id"}
                ],
            },
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"
    assert set(r.json()) == {"detail"}


def test_three_table_cycle_is_422():
    def t(name, ref):
        return {
            "name": name,
            "rows": 1,
            "fields": {"id": {"type": "int", "unique": True}},
            "foreign_keys": [
                {"column": f"{ref}_id", "references_table": ref,
                 "references_column": "id"}
            ],
        }

    body = {"tables": [t("a", "c"), t("b", "a"), t("c", "b")], "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "dependency_cycle"


def test_duplicate_table_name_is_422():
    body = {"tables": [id_table(name="u", rows=1), id_table(name="u", rows=1)],
            "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "duplicate_table"


def test_enum_requires_choices():
    body = {"tables": [{"name": "t", "rows": 1,
                        "fields": {"c": {"type": "enum"}}}], "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422


def test_bad_field_type_is_422():
    body = {"tables": [{"name": "t", "rows": 1,
                        "fields": {"c": {"type": "timestamp"}}}], "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422


def test_negative_rows_is_422():
    body = {"tables": [id_table(rows=-1)], "seed": 1}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422


def test_min_greater_than_max_is_422():
    body = {
        "tables": [
            {"name": "t", "rows": 1,
             "fields": {"n": {"type": "int", "min_value": 9, "max_value": 1}}}
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422


def test_unknown_unique_column_is_422():
    body = {
        "tables": [
            {"name": "t", "rows": 1,
             "fields": {"a": {"type": "int"}},
             "unique_constraints": [["a", "b"]]}
        ],
        "seed": 1,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "unknown_column"


def test_extra_field_forbidden():
    body = {"tables": [id_table(rows=1)], "seed": 1, "locale": "zh"}
    r = client.post("/fixtures", json=body)
    assert r.status_code == 422


def test_empty_body_is_422():
    assert client.post("/fixtures", json={}).status_code == 422
    assert client.post("/fixtures", json={"tables": []}).status_code == 422


# --------------------------------------------------------- reproducibility


def test_same_seed_is_reproducible():
    body = orders_body(seed=123)
    r1 = client.post("/fixtures", json=body)
    r2 = client.post("/fixtures", json=body)
    assert r1.json() == r2.json()


def test_string_seed_is_reproducible():
    body = orders_body()
    body["seed"] = "hello-fixture"
    r1 = client.post("/fixtures", json=body)
    r2 = client.post("/fixtures", json=body)
    assert r1.json() == r2.json()


def test_different_seeds_differ():
    b1 = orders_body(seed=1)
    b2 = orders_body(seed=2)
    r1 = client.post("/fixtures", json=b1).json()
    r2 = client.post("/fixtures", json=b2).json()
    assert r1["tables"] != r2["tables"]


def test_reordering_tables_does_not_change_per_table_data():
    body = orders_body(seed=77)
    r1 = client.post("/fixtures", json=body).json()["tables"]
    body_rev = {"tables": list(reversed(body["tables"])), "seed": 77}
    r2 = client.post("/fixtures", json=body_rev).json()["tables"]
    assert r1["users"] == r2["users"]
    assert r1["orders"] == r2["orders"]


# ------------------------------------------------------------------ types


def test_all_field_types_generate():
    body = {
        "tables": [
            {
                "name": "samples",
                "rows": 6,
                "fields": {
                    "i": {"type": "int", "min_value": 5, "max_value": 5},
                    "f": {"type": "float"},
                    "b": {"type": "boolean"},
                    "s": {"type": "string", "prefix": "p_", "min_length": 4,
                          "max_length": 4},
                    "n": {"type": "name"},
                    "e": {"type": "email", "prefix": "user"},
                    "u": {"type": "uuid"},
                    "d": {"type": "date"},
                    "dt": {"type": "datetime"},
                    "en": {"type": "enum", "choices": ["x", "y", "z"]},
                },
            }
        ],
        "seed": 2,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    row = r.json()["tables"]["samples"][0]
    assert row["i"] == 5
    assert isinstance(row["f"], float)
    assert isinstance(row["b"], bool)
    assert row["s"].startswith("p_") and len(row["s"]) == 6
    assert isinstance(row["n"], str) and " " in row["n"]
    assert row["e"].startswith("user") and row["e"].endswith("@example.com")
    assert len(row["u"]) == 36
    assert len(row["d"]) == 10
    assert len(row["dt"]) == 19
    assert row["en"] in {"x", "y", "z"}


def test_nullable_columns_can_be_null():
    body = {
        "tables": [
            {"name": "t", "rows": 200,
             "fields": {"c": {"type": "int", "nullable": True}}}
        ],
        "seed": 1,
    }
    rows = client.post("/fixtures", json=body).json()["tables"]["t"]
    assert any(r["c"] is None for r in rows)
    assert any(r["c"] is not None for r in rows)


def test_large_table_performance_and_uniqueness():
    body = {
        "tables": [
            id_table(name="users", rows=2000),
            {
                "name": "events",
                "rows": 5000,
                "fields": {"guid": {"type": "uuid", "unique": True}},
                "foreign_keys": [
                    {"column": "user_id", "references_table": "users",
                     "nullable": False}
                ],
            },
        ],
        "seed": 99,
    }
    r = client.post("/fixtures", json=body)
    assert r.status_code == 200, r.text
    data = r.json()["tables"]
    assert len({e["guid"] for e in data["events"]}) == 5000
    parent_ids = {u["id"] for u in data["users"]}
    assert all(e["user_id"] in parent_ids for e in data["events"])
