from app import synth

DOC = "dsid_00908a2dda4b4d359194a091019e8367"
DOC2 = "dsid_f9591843028149bdb47f7c3a70b3baa1"


def test_hnum_and_timestamps_are_deterministic():
    assert synth.hnum(DOC) == synth.hnum(DOC)
    assert synth.epoch(DOC) == synth.epoch(DOC)
    ts = synth.epoch(DOC)
    assert synth.BASE_EPOCH <= ts < synth.BASE_EPOCH + synth.TIME_RANGE


def test_distinct_docs_get_distinct_values():
    assert synth.slack_ts(DOC) != synth.slack_ts(DOC2)
    assert synth.github_number(DOC) != synth.github_number(DOC2)
    assert synth.confluence_id(DOC) != synth.confluence_id(DOC2)


def test_slack_ts_format():
    ts = synth.slack_ts(DOC)
    secs, micro = ts.split(".")
    assert secs.isdigit() and len(micro) == 6


def test_channel_id_stable_per_name():
    assert synth.slack_channel_id("general") == synth.slack_channel_id("general")
    assert synth.slack_channel_id("general") != synth.slack_channel_id("random")
    assert synth.slack_channel_id("general").startswith("C")


def test_time_formats():
    ts = 1712343600  # 2024-04-05T19:00:00Z
    assert synth.rfc3339(ts) == "2024-04-05T19:00:00Z"
    assert synth.rfc3339_millis(ts) == "2024-04-05T19:00:00.000Z"
    assert synth.jira_datetime(ts).endswith("+0000")
    assert synth.rfc2822(ts).endswith("+0000")


def test_account_id_and_login():
    assert synth.atlassian_account_id("ava.chen@x.com").startswith("5b")
    assert synth.github_login("ava.chen@x.com") == "ava-chen"
