from gb_fakes import message

from larkagentd.knowledge.discussions import reconstruct


def test_native_thread_participants_are_authors_and_not_mentioned_people():
    rows = [
        message(
            "om_example0001",
            thread_id="omt_example0001",
            sender_id="ou_alice1234",
            minute=0,
        ),
        message(
            "om_example0002",
            thread_id="omt_example0001",
            sender_id="ou_bob123456",
            minute=5,
        ),
    ]
    discussion = reconstruct(rows)[0]
    assert discussion.kind == "native_thread"
    assert discussion.participants == (("ou_alice1234", 1), ("ou_bob123456", 1))
    assert discussion.message_ids == ("om_example0001", "om_example0002")
    assert discussion.partial
    assert not reconstruct(rows, thread_complete={"omt_example0001"})[0].partial


def test_reply_from_only_bob_does_not_add_alice_as_participant():
    rows = [
        message("om_root0000001", sender_type="system", sender_id="ou_alice1234"),
        message(
            "om_reply000001",
            parent_id="om_root0000001",
            sender_id="ou_bob123456",
            minute=1,
        ),
    ]
    discussion = reconstruct(rows)[0]
    assert discussion.kind == "chat_window"
    assert discussion.participants == (("ou_bob123456", 1),)
    assert discussion.message_ids == ("om_reply000001",)
    assert discussion.native_thread_id is None


def test_gap_and_size_split_and_stable_key_with_late_reply():
    rows = [
        message(f"om_example{n:04d}", minute=m, text="ab" * 30)
        for n, m in ((1, 0), (2, 1), (3, 40))
    ]
    one = reconstruct(rows, max_messages=1)
    again = reconstruct(rows, max_messages=1)
    assert len(one) == 3
    assert [d.window_key for d in one] == [d.window_key for d in again]
    first_key = one[0].window_key
    late = message("om_example0004", minute=1, parent_id="om_example0001")
    assert reconstruct([*rows[:2], late], max_messages=3)[0].window_key == first_key
    assert reconstruct(rows, max_tokens=1)[0].message_ids == ("om_example0001",)
