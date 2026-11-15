"""Synthetic coverage for item metadata, search, and atomic patches."""

from contextlib import closing
import sqlite3

import pytest

from storage import Storage


@pytest.fixture
def inventory(tmp_path):
    storage = Storage(tmp_path / "synthetic-metadata.sqlite3")
    storage.record_user(101, "synthetic", "Synthetic")
    storage.record_user(202, "synthetic_outsider", "Synthetic")
    home = storage.create_household(101, "Synthetic home")
    return storage, storage.list_stores(home.id)[0].id


def snapshot(storage):
    with closing(storage._connect()) as connection:
        return tuple(
            tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"))
            for table in ("inventory_items", "item_events")
        )


def events(storage, item_id):
    with closing(storage._connect()) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM item_events WHERE item_id = ? ORDER BY id", (item_id,),
        )]


def test_additive_migration_preserves_legacy_columns_and_events(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic rice", 3, "кг", 0.5)
    with closing(storage._connect()) as connection, connection:
        connection.execute("UPDATE inventory_items SET notes = 'Synthetic historical note' WHERE id = ?", (item.id,))
        connection.execute("ALTER TABLE inventory_items DROP COLUMN synonyms")
    original = snapshot(storage)
    for _ in range(2):
        reopened = Storage(storage.path)
        with closing(reopened._connect()) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(inventory_items)")]
            assert columns.count("synonyms") == 1
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA foreign_key_list(item_events)").fetchone()[2] == "inventory_items"
        migrated = snapshot(reopened)
        assert tuple(row[:-1] for row in migrated[0]) == original[0]
        assert migrated[1] == original[1]
        assert reopened.get_item(item.id).notes == "Synthetic historical note"
        assert reopened.get_item(item.id).synonyms == ()
    reopened.update_item(store_id, 101, item.id, synonyms=["Рис", "  Крупа   рисовая ", "РИС"])
    assert Storage(storage.path).get_item(item.id).synonyms == ("Рис", "Крупа рисовая")


def test_failed_additive_migration_rolls_back(inventory, monkeypatch):
    storage, store_id = inventory
    storage.add_or_update_item(store_id, 101, "Synthetic item", 1, "шт", description="Synthetic note")
    with closing(storage._connect()) as connection, connection:
        connection.execute("ALTER TABLE inventory_items DROP COLUMN synonyms")
    before = storage.path.read_bytes()

    def fail(self, connection):
        raise sqlite3.OperationalError("Synthetic migration failure")

    monkeypatch.setattr(Storage, "_migrate_initial_extra_stores", fail)
    with pytest.raises(sqlite3.OperationalError, match="Synthetic migration failure"):
        Storage(storage.path)
    assert storage.path.read_bytes() == before


def test_unicode_literal_search_spans_fields_and_never_merges(inventory):
    storage, store_id = inventory
    first = storage.add_or_update_item(
        store_id, 101, "Рис", 1, "кг", description="Белая крупа ёж 100% A_B 'цитата'",
        synonyms=["Гарнир", "ЕЖ", "ёж"],
    )
    second = storage.add_or_update_item(store_id, 101, "Гречка", 2, "кг", synonyms=["Гарнир"])
    assert first.synonyms == ("Гарнир", "ЕЖ")
    for query in ("БЕЛАЯ", "ЕЖ", "рис гарнир крупа", "%", "_", "'ЦИТАТА'"):
        assert [item.id for item in storage.list_items(store_id, query=query)] == [first.id]
    assert {item.id for item in storage.list_items(store_id, query="ГАРНИР")} == {first.id, second.id}
    assert storage.list_items(store_id, query="белая гречка") == []
    assert len(storage.list_items(store_id, query=" \t ")) == 2
    assert storage.find_item(store_id, "Гарнир") is None
    third = storage.add_or_update_item(store_id, 101, "Гарнир", 3, "кг")
    assert third.id not in (first.id, second.id)
    assert len(storage.list_items(store_id)) == 3
    storage.archive_item(store_id, 101, first.id)
    assert storage.list_items(store_id, query="белая") == []
    assert storage.list_items(store_id, include_inactive=True, query="белая")[0].id == first.id


def test_metadata_omission_clearing_and_archived_readd(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(
        store_id, 101, "Synthetic item", 4, "кг", 2, description="Synthetic original", synonyms=["Alias"],
    )
    updated = storage.update_item(store_id, 101, item.id, description="Synthetic replacement")
    assert (updated.quantity, updated.min_quantity, updated.unit, updated.synonyms) == (4, 2, "кг", ("Alias",))
    added = storage.add_or_update_item(store_id, 101, item.name, 1, "кг")
    assert (added.quantity, added.min_quantity, added.notes, added.synonyms) == (5, 2, "Synthetic replacement", ("Alias",))
    storage.archive_item(store_id, 101, item.id)
    restored = storage.add_or_update_item(store_id, 101, item.name, 7, "кг")
    assert (restored.quantity, restored.min_quantity, restored.notes, restored.synonyms) == (7, 2, "Synthetic replacement", ("Alias",))
    cleared = storage.update_item(store_id, 101, item.id, description="", synonyms=[])
    assert (cleared.notes, cleared.synonyms) == ("", ())
    storage.add_or_update_item(store_id, 101, item.name, 0, "кг", description="Replacement", synonyms=["Other"])
    cleared_by_add = storage.add_or_update_item(store_id, 101, item.name, 0, "кг", description="", synonyms=[])
    assert (cleared_by_add.notes, cleared_by_add.synonyms) == ("", ())


def test_combined_update_records_one_safe_event_and_noop_changes_nothing(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic old", 2, "кг", 0.5)
    updated = storage.update_item(
        store_id, 101, item.id, name="  Synthetic   new ", quantity=7, min_quantity=1,
        description="Synthetic private metadata", synonyms=["Synthetic private alias"], expected_unit="кг",
    )
    assert (updated.name, updated.normalized_name, updated.quantity, updated.min_quantity) == ("Synthetic new", "synthetic new", 7, 1)
    audit = events(storage, item.id)
    assert len(audit) == 2
    assert (audit[-1]["action"], audit[-1]["delta"]) == ("update", 5)
    assert audit[-1]["note"] == "Changed fields: name, quantity, min_quantity, description, synonyms"
    assert "private" not in audit[-1]["note"]
    before = snapshot(storage)
    assert storage.update_item(store_id, 101, item.id) == updated
    assert storage.update_item(
        store_id, 101, item.id, name=updated.name, quantity=7, min_quantity=1,
        description=updated.notes, synonyms=list(updated.synonyms),
    ) == updated
    assert snapshot(storage) == before


def test_rename_collision_and_stale_unit_roll_back_every_field(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic first", 2, "кг", 0.5)
    collision = storage.add_or_update_item(store_id, 101, "Synthetic other", 1, "кг")
    storage.archive_item(store_id, 101, collision.id)
    before = snapshot(storage)
    with pytest.raises(ValueError, match="already exists"):
        storage.update_item(store_id, 101, item.id, name="SYNTHETIC OTHER", quantity=99, description="changed", synonyms=["new"])
    assert snapshot(storage) == before
    with pytest.raises(ValueError, match="unit changed"):
        storage.update_item(store_id, 101, item.id, quantity=99, description="changed", expected_unit="г")
    assert snapshot(storage) == before


def test_audit_failure_rolls_back_entire_combined_patch(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic item", 2, "кг")
    with closing(storage._connect()) as connection, connection:
        connection.execute("""CREATE TRIGGER synthetic_update_failure BEFORE INSERT ON item_events
            WHEN NEW.action = 'update' BEGIN SELECT RAISE(ABORT, 'Synthetic audit failure'); END""")
    before = snapshot(storage)
    with pytest.raises(sqlite3.IntegrityError, match="Synthetic audit failure"):
        storage.update_item(
            store_id, 101, item.id, name="Synthetic renamed", quantity=99, min_quantity=3,
            description="Synthetic replacement", synonyms=["Synthetic alias"],
        )
    assert snapshot(storage) == before


def test_all_item_read_paths_include_aliases(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic item", 1, "кг", 2, synonyms=["Alias"])
    assert storage.get_item(item.id).synonyms == ("Alias",)
    assert storage.list_items(store_id)[0].synonyms == ("Alias",)
    assert storage.low_items(store_id)[0].synonyms == ("Alias",)
    assert storage.low_items_by_store(item.household_id)[0][1][0].synonyms == ("Alias",)
    assert storage.find_item(store_id, item.name).synonyms == ("Alias",)


def test_membership_store_binding_and_archive_guards(inventory):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic item", 2, "кг")
    before = snapshot(storage)
    assert storage.update_item(store_id, 202, item.id, quantity=99) is None
    assert storage.update_item(store_id + 1, 101, item.id, quantity=99) is None
    assert storage.update_item(store_id, 101, 99999, quantity=99) is None
    assert snapshot(storage) == before
    storage.archive_item(store_id, 101, item.id)
    before = snapshot(storage)
    with pytest.raises(ValueError, match="Archived"):
        storage.update_item(store_id, 101, item.id, description="changed")
    assert snapshot(storage) == before


@pytest.mark.parametrize("patch", [
    {"quantity": True}, {"quantity": float("nan")}, {"quantity": float("inf")},
    {"quantity": -1}, {"quantity": "2"}, {"quantity": 10**400}, {"min_quantity": False}, {"min_quantity": -1},
    {"description": 42}, {"description": "x" * 4001}, {"synonyms": "alias"},
    {"synonyms": [1]}, {"synonyms": [" "]}, {"synonyms": ["x" * 201]},
    {"synonyms": ["x"] * 51}, {"name": " "}, {"name": 42},
])
def test_invalid_update_patch_has_no_side_effects(inventory, patch):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic item", 2, "кг")
    before = snapshot(storage)
    with pytest.raises(ValueError):
        storage.update_item(store_id, 101, item.id, **patch)
    assert snapshot(storage) == before


@pytest.mark.parametrize("metadata", [
    {"description": "x" * 4001}, {"description": 42}, {"synonyms": "alias"},
    {"synonyms": [False]}, {"synonyms": [" "]}, {"synonyms": ["x" * 201]}, {"synonyms": ["x"] * 51},
])
def test_invalid_add_metadata_does_not_add_stock(inventory, metadata):
    storage, store_id = inventory
    storage.add_or_update_item(store_id, 101, "Synthetic item", 2, "кг")
    before = snapshot(storage)
    with pytest.raises(ValueError):
        storage.add_or_update_item(store_id, 101, "Synthetic item", 99, "кг", **metadata)
    assert snapshot(storage) == before


@pytest.mark.parametrize("patch", [{"description": "Synthetic changed"}, {"synonyms": ["Synthetic alias"]}])
def test_metadata_invalidates_reviewed_deletion_preview(inventory, patch):
    storage, store_id = inventory
    item = storage.add_or_update_item(store_id, 101, "Synthetic item", 2, "кг")
    preview = storage.preview_deletion(101, "item", item.id)
    storage.update_item(store_id, 101, item.id, **patch)
    with pytest.raises(ValueError):
        storage.delete_reviewed(101, "item", item.id, preview["preview_token"])
    assert storage.get_item(item.id) is not None
