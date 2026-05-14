from types import SimpleNamespace

from andon_system.services import radius_service


def test_resolve_radius_machine_id_uses_explicit_mapping():
    machine = SimpleNamespace(radius_machine_id=999, machine_type="Press", name="Press 9", machine_code="PRESS-9")

    assert radius_service.resolve_radius_machine_id(machine) == 999


def test_resolve_radius_machine_id_uses_special_press_two_rule():
    machine = SimpleNamespace(radius_machine_id=None, machine_type="Press", name="Press 2", machine_code="PRESS-2")

    assert radius_service.resolve_radius_machine_id(machine) == 201


def test_resolve_radius_machine_id_uses_default_press_rule():
    machine = SimpleNamespace(radius_machine_id=None, machine_type="Press", name="Press 6", machine_code="PRESS-6")

    assert radius_service.resolve_radius_machine_id(machine) == 206


def test_resolve_radius_machine_id_skips_press_one():
    machine = SimpleNamespace(radius_machine_id=None, machine_type="Press", name="Press 1", machine_code="PRESS-1")

    assert radius_service.resolve_radius_machine_id(machine) is None


def test_resolve_radius_machine_id_skips_non_press_machines():
    machine = SimpleNamespace(radius_machine_id=None, machine_type="Extrusion", name="Extrusion 2", machine_code="EXTRUSION-2")

    assert radius_service.resolve_radius_machine_id(machine) is None


def test_build_radius_status_map_batches_lookup(monkeypatch):
    machines = [
        SimpleNamespace(id=10, radius_machine_id=None, machine_type="Press", name="Press 2", machine_code="PRESS-2"),
        SimpleNamespace(id=11, radius_machine_id=206, machine_type="Press", name="Press 6", machine_code="PRESS-6"),
        SimpleNamespace(id=12, radius_machine_id=None, machine_type="Bag Machine", name="Bag 1", machine_code="BAG-1"),
    ]

    def fake_fetch(radius_machine_ids):
        assert radius_machine_ids == {201, 206}
        return {
            201: {
                "machine_id": 201,
                "operation_code": "OP-2",
                "job_code": "JOB-2",
                "status_code": "S1",
                "status_description": "Running",
                "event_type": "R",
            },
            206: {
                "machine_id": 206,
                "operation_code": "OP-6",
                "job_code": "JOB-6",
                "status_code": "S3",
                "status_description": "Cleaning",
                "event_type": "C",
            },
        }

    monkeypatch.setattr(radius_service, "_fetch_radius_rows", fake_fetch)

    status_map = radius_service.build_radius_status_map(machines)

    assert status_map[10]["machine_id"] == 201
    assert status_map[10]["operation_code"] == "OP-2"
    assert status_map[10]["status_label"] == "S1 - Running"
    assert status_map[11]["machine_id"] == 206
    assert status_map[11]["job_code"] == "JOB-6"
    assert 12 not in status_map
