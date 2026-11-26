import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace as N
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select, text
from test_core import store as store_fixture
from test_dashboard import ORIGIN, change, login
from test_dashboard import access as access_fixture

from larkagentd.core.store import directory_metadata
from larkagentd.gateway.access import ACCESS_LOCK
from larkagentd.gateway.dashboard import PREFIX, make_handler
from larkagentd.gateway.directory import Directory
from larkagentd.lark.adapter import Lark
from larkagentd.lark.profile import avatar_url

store = store_fixture
access = access_fixture


class Profiles:
    def __init__(self):
        self.calls = []
        self.fail = False

    def user_profile_by_email(self, email):
        self.calls.append(("email", email))
        if self.fail:
            raise RuntimeError("sensitive upstream body")
        if email == "missing@example.com":
            return None
        return {
            "name": "Example Person",
            "email": email,
            "avatar_url": "https://images.example.com/person.png",
            "secret": "must-not-store",
        }

    def user_profile(self, open_id):
        self.calls.append(("owner", open_id))
        return {
            "name": "Owner Profile",
            "email": "owner@example.com",
            "avatar_url": "https://images.example.com/owner.png",
        }

    def group_profile(self, chat_id):
        self.calls.append(("group", chat_id))
        if self.fail:
            raise RuntimeError("sensitive upstream body")
        return {
            "name": "Example Group",
            "avatar_url": "https://images.example.com/group.png",
        }


@pytest.fixture
def directory(store):
    adapter = Profiles()
    service = Directory(store, adapter)
    yield service, adapter
    service.close()


@pytest.fixture
def directory_http(access):
    adapter = Profiles()
    service = Directory(access.store, adapter)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(access, ORIGIN, service)
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{server.server_port}",
        headers={"Origin": ORIGIN, "Host": ORIGIN.split("://", 1)[1]},
    ) as client:
        yield access, client, adapter, service
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
    service.close()


def test_names_emails_and_avatars_for_users_and_groups_are_cached(directory):
    service, adapter = directory
    body = {"emails": ["PERSON@EXAMPLE.COM"], "owners": ["t:ou_owner"]}
    users = service.lookup("users", body)["items"]
    assert users[0]["email"] == "person@example.com"
    assert users[0]["name"] == "Example Person"
    assert users[0]["avatar_url"] == "https://images.example.com/person.png"
    assert (
        users[1]["name"] == "Owner Profile" and users[1]["email"] == "owner@example.com"
    )
    assert all(item["metadata_status"] == "available" for item in users)
    service.lookup("users", body)
    assert len(adapter.calls) == 2
    [group] = service.lookup("groups", {"group_ids": ["oc_room"]})["items"]
    assert group["name"] == "Example Group" and group["avatar_url"].endswith(
        "group.png"
    )
    assert group["group_id"] == "oc_room"
    with service.store.engine.begin() as conn:
        assert all(
            "secret" not in row
            for row in conn.scalars(select(directory_metadata.c.data))
        )


def test_missing_and_system_profiles_are_explicit_without_fake_people(directory):
    service, adapter = directory
    items = service.lookup(
        "users",
        {"emails": ["missing@example.com"], "owners": ["system:skill-promotion"]},
    )["items"]
    assert items[0]["metadata_status"] == "not_found" and items[0]["name"] is None
    assert (
        items[1]["metadata_status"] == "not_applicable" and items[1]["name"] == "System"
    )
    assert len(adapter.calls) == 1


def test_permission_failure_returns_stale_last_known_metadata(directory, capsys):
    service, adapter = directory
    service.lookup("groups", {"group_ids": ["oc_room"]})
    adapter.fail = True
    [group] = service.lookup("groups", {"group_ids": ["oc_room"], "refresh": True})[
        "items"
    ]
    assert group["metadata_status"] == "stale" and group["name"] == "Example Group"
    assert "sensitive upstream body" not in capsys.readouterr().err


def test_unavailable_missing_profile_stays_nullable(directory):
    service, adapter = directory
    adapter.fail = True
    [item] = service.lookup("users", {"emails": ["person@example.com"]})["items"]
    assert item["metadata_status"] == "unavailable"
    assert item["name"] is None and item["avatar_url"] is None
    assert item["email"] == "person@example.com"


def test_cache_persists_across_service_instances(directory):
    service, adapter = directory
    service.lookup("groups", {"group_ids": ["oc_room"]})
    fresh = Directory(service.store, Profiles())
    try:
        assert (
            fresh.lookup("groups", {"group_ids": ["oc_room"]})["items"][0]["name"]
            == "Example Group"
        )
        assert fresh.transport.calls == []
    finally:
        fresh.close()


def test_pending_refresh_is_bounded_and_deduplicated(store):
    entered, release = threading.Event(), threading.Event()

    class Slow(Profiles):
        def user_profile_by_email(self, email):
            self.calls.append(("email", email))
            entered.set()
            assert release.wait(5)
            return {"name": "Delayed Person", "email": email}

    adapter = Slow()
    service = Directory(store, adapter, workers=1, wait_seconds=0)
    try:
        body = {"emails": ["person@example.com"]}
        result = service.lookup("users", body)
        assert entered.wait(2)
        assert result["items"][0]["metadata_status"] == "pending"
        assert result["retry_after_seconds"] == 2
        service.lookup("users", body)
        assert len(adapter.calls) == 1
        assert all(worker.daemon for worker in service.workers)
        release.set()
        service.tasks.join()
        assert service.lookup("users", body)["items"][0]["name"] == "Delayed Person"
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize(
    "resource,body",
    [
        ("users", {}),
        ("users", {"emails": "x"}),
        ("users", {"emails": ["bad"]}),
        ("users", {"owners": ["invalid"]}),
        ("users", {"emails": ["person@example.com"], "refresh": "true"}),
        ("groups", {"group_ids": ["../bad"]}),
        ("groups", {"group_ids": ["g"] * 21}),
        ("users", {"emails": ["p@example.com"], "role": "admin"}),
    ],
)
def test_invalid_lookup_inputs_are_rejected(directory, resource, body):
    from larkagentd.gateway.access import AccessError

    with pytest.raises(AccessError) as error:
        directory[0].lookup(resource, body)
    assert error.value.status == 400 and directory[1].calls == []


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://images.example.com/x",
        "https://name:password@images.example.com/x",
        "https://images.example.com/x?access_token=secret",
        "https://images.example.com/x?ACCESS_TOKEN=secret",
    ],
)
def test_unsafe_avatar_urls_are_not_returned(url):
    assert avatar_url(url) is None


def test_metadata_requires_admin_manager_cookie_and_same_origin(directory_http):
    access, client, adapter, service = directory_http
    path = PREFIX + "/directory/users"
    body = {"emails": ["person@example.com"]}
    assert client.post(path, json=body).status_code == 401
    assert adapter.calls == []
    change(access, "PUT", "groups/oc_test/members/user@example.com")
    login(access, client)
    assert (
        client.post(
            path, headers={"Origin": "https://evil.example.com"}, json=body
        ).status_code
        == 403
    )
    assert client.post(path, json=body).json()["items"][0]["name"] == "Example Person"
    change(access, "PUT", "managers/user@example.com")
    client.cookies.clear()
    login(access, client, "t:u")
    assert (
        client.post(
            PREFIX + "/directory/groups", json={"group_ids": ["oc_room"]}
        ).status_code
        == 200
    )
    change(access, "DELETE", "managers/user@example.com")
    assert client.post(path, json=body).status_code == 401


def test_directory_network_reads_do_not_hold_policy_lock(directory_http):
    access, client, adapter, _ = directory_http
    original = adapter.group_profile

    def profile(key):
        with access.store.engine.begin() as conn:
            assert conn.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ACCESS_LOCK}
            )
        return original(key)

    adapter.group_profile = profile
    login(access, client)
    assert (
        client.post(
            PREFIX + "/directory/groups", json={"group_ids": ["oc_room"]}
        ).json()["items"][0]["metadata_status"]
        == "available"
    )


def test_official_sdk_adapter_requests_and_whitelists_profile_fields():
    transport = object.__new__(Lark)
    transport.client = Mock()
    person = N(
        name="Lark Name",
        enterprise_email="PERSON@EXAMPLE.COM",
        email="other@example.com",
        avatar=N(
            avatar_240="https://images.example.com/a",
            avatar_72=None,
            avatar_640=None,
            avatar_origin=None,
        ),
        mobile="private",
    )
    transport.client.contact.v3.user.get.return_value = N(
        success=lambda: True, data=N(user=person)
    )
    transport.client.contact.v3.user.batch_get_id.return_value = N(
        success=lambda: True,
        data=N(user_list=[N(email="person@example.com", user_id="ou_person")]),
    )
    got = transport.user_profile_by_email("person@example.com")
    assert got == {
        "name": "Lark Name",
        "email": "person@example.com",
        "avatar_url": "https://images.example.com/a",
    }
    request = transport.client.contact.v3.user.batch_get_id.call_args.args[0]
    assert (
        request.request_body.emails == ["person@example.com"]
        and request.user_id_type == "open_id"
    )
    request = transport.client.contact.v3.user.get.call_args.args[0]
    assert request.user_id == "ou_person" and request.user_id_type == "open_id"
    transport.client.im.v1.chat.get.return_value = N(
        success=lambda: True,
        data=N(name="Lark Group", avatar="https://images.example.com/g"),
    )
    assert transport.group_profile("oc_room")["name"] == "Lark Group"
    assert transport.client.im.v1.chat.get.call_args.args[0].chat_id == "oc_room"


def test_sdk_failures_and_scope_absence_do_not_invent_profiles():
    transport = object.__new__(Lark)
    transport.client = Mock()
    transport.client.contact.v3.user.batch_get_id.return_value = N(
        success=lambda: True, data=N(user_list=[])
    )
    assert transport.user_profile_by_email("missing@example.com") is None
    transport.client.im.v1.chat.get.return_value = N(success=lambda: False, data=None)
    with pytest.raises(RuntimeError, match="metadata unavailable"):
        transport.group_profile("oc_room")
