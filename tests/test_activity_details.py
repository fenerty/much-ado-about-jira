from datetime import timedelta
from activity_details import MAX_CHANGES, ado_actor, ado_changes
from tests.test_store import make_item, result, NOW
from store import Store


def test_source_details_do_not_copy_rich_text_or_raw_identities():
    details = ado_changes({'fields': {
        'System.State': {'oldValue': 'New', 'newValue': 'Active'},
        'System.AssignedTo': {'oldValue': None, 'newValue': {'displayName': 'Example Engineer', 'uniqueName': 'private@example.test'}},
        'System.Description': {'newValue': '<p>private full body</p>'},
    }})
    assert details == ['Status: New → Active', 'Assignee: None → Example Engineer', 'Description changed']
    assert 'private' not in str(details)


def test_schedule_dates_are_readable_without_timezone_date_shift():
    details = ado_changes({'fields': {
        'Microsoft.VSTS.Scheduling.StartDate': {'oldValue': '2026-05-26T00:00:00Z', 'newValue': '2026-07-21T00:00:00Z'},
        'Microsoft.VSTS.Scheduling.FinishDate': {'newValue': '2026-09-01T00:00:00Z'},
        'Microsoft.VSTS.Scheduling.TargetDate': {'oldValue': '2026-07-06T00:00:00Z', 'newValue': '2026-09-01T00:00:00Z'},
    }})
    assert details == [
        'Start Date: May 26, 2026 → July 21, 2026',
        'Finish Date: None → September 1, 2026',
        'Target Date: July 6, 2026 → September 1, 2026',
    ]


def test_schedule_dates_preserve_nonmidnight_source_times():
    assert ado_changes({'fields': {
        'Microsoft.VSTS.Scheduling.StartDate': {'oldValue': '2026-05-26T06:00:00Z', 'newValue': '2026-07-21T12:00:00Z'},
    }}) == ['Start Date: May 26, 2026 at 06:00:00 UTC → July 21, 2026 at 12:00:00 UTC']


def test_custom_metadata_supports_types_names_and_explicit_time_offsets():
    details = ado_changes({'fields': {
        'Custom.ReadyAt': {'oldValue': '2026-07-21T14:35:12-04:00', 'newValue': '2026-07-21T18:35:12Z'},
        'Custom.Effort': {'oldValue': 0, 'newValue': 2.5},
        'Custom.Approved': {'oldValue': False, 'newValue': True},
        'Custom.Ready': {'oldValue': None, 'newValue': False},
    }}, {
        'Custom.ReadyAt': {'name': 'Ready at', 'type': 'dateTime'},
        'Custom.Effort': {'name': 'Planned effort', 'type': 'double'},
        'Custom.Approved': {'name': 'Approved', 'type': 'boolean'},
    })
    assert details == [
        'Ready at: July 21, 2026 at 14:35:12 -04:00 → July 21, 2026 at 18:35:12 UTC',
        'Planned effort: 0 → 2.5', 'Approved: No → Yes', 'Ready: None → No',
    ]


def test_unset_cleared_zero_and_false_are_distinct():
    details = ado_changes({'fields': {
        'Custom.Zero': {'newValue': 0}, 'Custom.Cleared': {'oldValue': 0},
        'Custom.Disabled': {'oldValue': False, 'newValue': None},
        'Custom.ChangedType': {'oldValue': 0, 'newValue': False},
    }})
    assert details == ['Zero: None → 0', 'Cleared: 0 → None', 'Disabled: No → None', 'Changed Type: 0 → No']


def test_custom_field_fallback_and_familiar_labels():
    details = ado_changes({'fields': {
        'Custom.ReleaseTrain': {'oldValue': 'Alpha', 'newValue': 'Beta'},
        'System.State': {'oldValue': 'New', 'newValue': 'Active'},
        'System.WorkItemType': {'oldValue': 'Task', 'newValue': 'Bug'},
    }}, {'System.State': {'name': 'State', 'type': 'string'},
         'Custom.ReleaseTrain': {'type': 'string'}, 'System.WorkItemType': {'type': 'string'}})
    assert details == ['Status: New → Active', 'Release Train: Alpha → Beta', 'Work Item Type: Task → Bug']


def test_unknown_complex_rich_text_and_long_strings_are_label_only():
    details = ado_changes({'fields': {
        'Custom.Html': {'newValue': '<p>private body</p>'},
        'Custom.Details': {'newValue': {'secret': 'private body'}},
        'Custom.Paragraph': {'newValue': 'private body\nsecond line'},
        'Custom.LongText': {'newValue': 'private body ' * 50},
        'Custom.Rich': {'newValue': 'private body'},
        'Custom.NamedObject': {'newValue': {'name': 'private body', 'details': 'private body'}},
    }}, {'Custom.Rich': {'name': 'Rich field', 'type': 'html'}})
    assert details == ['Html changed', 'Details changed', 'Paragraph changed', 'Long Text changed', 'Rich field changed', 'Named Object changed']
    assert 'private' not in str(details)


def test_metadata_outage_does_not_copy_unknown_short_text_bodies():
    assert ado_changes({'fields': {
        'Custom.Notes': {'oldValue': 'private before', 'newValue': 'private after'},
        'Custom.Effort': {'oldValue': 0, 'newValue': 1},
        'System.State': {'oldValue': 'New', 'newValue': 'Active'},
    }}) == ['Status: New → Active', 'Notes changed', 'Effort: 0 → 1']


def test_identity_guids_and_unexpected_nested_display_names_are_not_copied():
    details = ado_changes({'fields': {
        'System.AssignedTo': {'oldValue': '11111111-2222-3333-4444-555555555555', 'newValue': {'displayName': {'secret': 'private'}}},
    }})
    assert details == ['Assignee: (identity) → (identity)']


def test_ado_actor_only_returns_bounded_display_names_without_contacts_or_ids():
    assert ado_actor({'displayName': 'Example Engineer', 'uniqueName': 'private@example.test'}) == 'Example Engineer'
    assert ado_actor({'name': 'Example Engineer <private@example.test>'}) == 'Example Engineer'
    assert ado_actor('Example Engineer (private@example.test)') == 'Example Engineer'
    assert ado_actor({'displayName': 'private@example.test'}) is None
    assert ado_actor({'uniqueName': 'private@example.test', 'id': 'private-id'}) is None
    assert ado_actor('11111111-2222-3333-4444-555555555555') is None
    assert ado_actor({'displayName': {'secret': 'private'}}) is None
    assert ado_actor(None) is None
    assert len(ado_actor({'displayName': 'A' * 200})) == 120


def test_date_time_fraction_is_not_lost():
    assert ado_changes({'fields': {'Custom.At': {'newValue': '2026-07-21T14:35:12.123400Z'}}},
                       {'Custom.At': {'type': 'dateTime'}}) == ['At: None → July 21, 2026 at 14:35:12.1234 UTC']


def test_identity_display_names_never_include_contact_or_raw_identity_fields():
    details = ado_changes({'fields': {
        'Custom.Owner': {'oldValue': 'Old Name <private@example.test>', 'newValue': {'displayName': 'New Name (private@example.test)', 'uniqueName': 'private@example.test', 'id': 'private-id'}},
        'System.AssignedTo': {'oldValue': {'displayName': 'private@example.test'}, 'newValue': {'id': 'private-id', 'uniqueName': 'private@example.test'}},
    }}, {'Custom.Owner': {'name': 'Owner', 'type': 'identity'}})
    assert details == ['Assignee: (identity) → (identity)', 'Owner: Old Name → New Name']
    assert 'private' not in str(details)


def test_administrative_and_generated_board_tracking_changes_do_not_make_noise():
    assert ado_changes({'fields': {
        'System.Rev': {'oldValue': 15, 'newValue': 16},
        'System.ChangedDate': {'newValue': '2026-07-21T18:35:12Z'},
        'System.ChangedBy': {'newValue': {'displayName': 'Engineer'}},
        'Microsoft.VSTS.Common.StateChangeDate': {'newValue': '2026-07-21T18:35:12Z'},
        'WEF_ABC_Kanban.Column': {'oldValue': 'A', 'newValue': 'B'},
        'System.PersonId': {'newValue': 42}, 'System.AreaId': {'newValue': 3},
        'System.IterationId': {'newValue': 4}, 'System.AreaLevel1': {'newValue': 'One'},
        'System.AreaLevel7': {'newValue': 'Seven'}, 'System.IterationLevel3': {'newValue': 'Three'},
    }}) == []


def test_unchanged_or_malformed_fields_are_ignored():
    assert ado_changes({'fields': {
        'System.State': {'oldValue': 'Active', 'newValue': 'Active'},
        'Custom.Bad': 'not a delta', 'Custom.Empty': {},
    }}, {'Custom.Bad': 42}) == []
    assert ado_changes({'fields': 'not an object'}) == []


def test_relation_counts_are_bounded_without_copying_link_or_attachment_data():
    details = ado_changes({'relations': {'added': [{'url': 'private'}] * 3, 'removed': [{'url': 'private'}], 'updated': []}})
    assert details == ['Links or attachments: 3 added, 1 removed']
    assert ado_changes({'relations': ['private']}) == ['Links or attachments changed']


def test_large_change_sets_have_a_bounded_summary():
    details = ado_changes({'fields': {f'Custom.Field{i}': {'newValue': i} for i in range(MAX_CHANGES + 10)}})
    assert len(details) == MAX_CHANGES
    assert details[-1] == '11 other fields changed'


def test_snapshot_changes_persist_across_refreshes_and_do_not_invent_baseline(tmp_path):
    store = Store(tmp_path / 'test.sqlite3')
    store.initialize()
    store.replace_connector(result(make_item()), 60)
    assert store.load()[1][0].changes == []
    next_result = result(make_item('Blocked', NOW + timedelta(hours=1)))
    store.replace_connector(next_result, 60)
    event = next(event for event in store.load()[1] if event.timestamp == NOW + timedelta(hours=1))
    assert event.changes == ['Status: Active → Blocked']
    assert event.detail_source == 'Between local refreshes'
    store.set_local_state(event.id, 'seen')
    store.replace_connector(result(make_item('Blocked', NOW + timedelta(hours=1))), 60)
    event = next(event for event in store.load()[1] if event.timestamp == NOW + timedelta(hours=1))
    assert event.changes == ['Status: Active → Blocked']
    assert not event.unread
