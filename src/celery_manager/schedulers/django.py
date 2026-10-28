from dataclasses import replace

from .base import ScheduleEntry


class DjangoCeleryBeatBackend:
    name = "django-beat"
    writable = True

    def __init__(self, celery_app=None):
        from django_celery_beat.models import PeriodicTask
        from django_celery_beat.schedulers import ModelEntry
        self.app, self.Model, self.Entry = celery_app, PeriodicTask, ModelEntry

    def _convert(self, model):
        entry = self.Entry(model, app=self.app)
        options = dict(entry.options)
        options.get("headers", {}).pop("periodic_task_name", None)
        return ScheduleEntry(entry.name, entry.task, entry.schedule, list(entry.args), dict(entry.kwargs),
                             options, model.enabled, model.last_run_at, self.name)

    def list(self):
        return [self._convert(m) for m in self.Model.objects.all()]

    def get(self, name):
        model = self.Model.objects.filter(name=name).first()
        return self._convert(model) if model else None

    def _fields(self, entry):
        from datetime import datetime
        fields = self.Entry._unpack_fields(task=entry.task, schedule=entry.schedule,
                                           args=entry.args, kwargs=entry.kwargs, options=entry.options,
                                           enabled=entry.enabled)
        expires = entry.options.get("expires")
        fields["expires"] = None
        fields["expire_seconds"] = None
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if isinstance(expires, datetime):
            fields["expires"] = expires
        elif expires is not None:
            if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
                raise ValueError("Django schedule expiry must be an ISO timestamp or nonnegative whole seconds")
            fields["expire_seconds"] = expires
        return fields

    def create(self, entry):
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction
        try:
            with transaction.atomic():
                model = self.Model.objects.create(name=entry.name, **self._fields(entry))
        except (IntegrityError, ValidationError) as exc:
            # PeriodicTask.save() validates uniqueness before INSERT. Normalise
            # both that path and a concurrent database uniqueness violation,
            # without mislabelling unrelated validation/storage errors.
            if self.Model.objects.filter(name=entry.name).exists():
                raise FileExistsError(entry.name) from exc
            if isinstance(exc, ValidationError):
                raise ValueError(str(exc)) from exc  # noqa: TRY004 — invalid model values, not a Python argument type
            raise
        return self._convert(model)

    def update(self, name, **changes):
        from django.db import transaction
        with transaction.atomic():
            model = self.Model.objects.select_for_update().filter(name=name).first()
            if model is None:
                raise KeyError(name)
            entry = replace(self._convert(model), **changes)
            for key, value in self._fields(entry).items():
                setattr(model, key, value)
            model.save()  # bumps PeriodicTasks.changed through the model signal
            return self._convert(model)

    def delete(self, name):
        model = self.Model.objects.filter(name=name).first()
        if model is None:
            raise KeyError(name)
        model.delete()

    def beat_scheduler_class(self):
        return "celery_manager.beat:DjangoDatabaseScheduler"
