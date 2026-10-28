"""Adapter errors must have consistent semantics across Django save paths."""
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from celery_manager.schedulers.base import ScheduleEntry
from celery_manager.schedulers.django import DjangoCeleryBeatBackend


@pytest.mark.parametrize("error", [ValidationError({"name": ["already exists"]}), IntegrityError("duplicate")])
def test_duplicate_create_is_normalised_only_for_existing_name(error):
    adapter = DjangoCeleryBeatBackend.__new__(DjangoCeleryBeatBackend)
    adapter.Model = MagicMock()
    adapter.Model.objects.create.side_effect = error
    adapter.Model.objects.filter.return_value.exists.return_value = True
    adapter._fields = MagicMock(return_value={})
    with patch("django.db.transaction.atomic", return_value=nullcontext()), pytest.raises(FileExistsError):
        adapter.create(ScheduleEntry("duplicate", "task", None))
    adapter.Model.objects.filter.assert_called_once_with(name="duplicate")


@pytest.mark.parametrize("error, expected", [(ValidationError("invalid schedule"), ValueError),
                                             (IntegrityError("unrelated constraint"), IntegrityError)])
def test_nonduplicate_error_is_not_labelled_name_conflict(error, expected):
    adapter = DjangoCeleryBeatBackend.__new__(DjangoCeleryBeatBackend)
    adapter.Model = MagicMock()
    adapter.Model.objects.create.side_effect = error
    adapter.Model.objects.filter.return_value.exists.return_value = False
    adapter._fields = MagicMock(return_value={})
    with patch("django.db.transaction.atomic", return_value=nullcontext()), pytest.raises(expected) as raised:
        adapter.create(ScheduleEntry("new", "task", None))
    assert not isinstance(raised.value, FileExistsError)
