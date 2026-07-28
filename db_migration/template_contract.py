"""Canonical storage contract for versioned task templates.

This module is deliberately side-effect free so the offline migration harness,
reconciliation report, and tests share the same controlled vocabularies.
Runtime code keeps its own SQLite bootstrap implementation.
"""

from __future__ import annotations


TASK_TEMPLATE_ORIGINS = ("system", "manual")
TASK_TEMPLATE_STATUSES = ("active", "archived")
TASK_TEMPLATE_EVENT_TYPES = (
    "created", "version_created", "archived", "activated",
)
TASK_TEMPLATE_MODES = ("open", "personal", "all")
TASK_TEMPLATE_EVIDENCE_POLICIES = (
    "none", "comment_only", "photo_required", "before_after",
)
TASK_TEMPLATE_TASK_TYPES = (
    "relocate", "fix_zone", "charge", "rescue",
    "community", "referral", "photo_check",
)


SYSTEM_TEMPLATE_SEEDS = (
    {
        "id": "f679a68c-ef2a-561f-b191-96fc89b306e4",
        "version_id": "455586eb-d473-5c43-b522-a3b093bfd5af",
        "key": "parking",
        "title": "Поправить парковку байков",
        "task_type": "fix_zone",
        "task_title": "Поправить парковку байков",
        "details": (
            "Аккуратно выровнять байки, освободить проход и приложить "
            "фотоотчёт в задании."
        ),
        "reward": 80,
        "mode": "open",
        "evidence_policy": "photo_required",
        "max_participants": 1,
        "budget_cap": 80,
        "content_hash": (
            "3a57569012e4a11f73067b4b524311c8517c4f78ce78e8852eacbcdbf929acc2"
        ),
    },
    {
        "id": "2f6cee00-51f1-5cf2-9dda-487711836f2f",
        "version_id": "81cfc2f7-6106-5707-add0-4234710e85a0",
        "key": "parking_photo",
        "title": "Проверить парковку и сделать фото",
        "task_type": "photo_check",
        "task_title": "Фото-проверка парковки",
        "details": "Проверить состояние парковки и отправить понятное фото результата.",
        "reward": 50,
        "mode": "all",
        "evidence_policy": "photo_required",
        "max_participants": 10,
        "budget_cap": 500,
        "content_hash": (
            "cd85abc8d5a1dee6e80bac320abe939da8bdecb2e7627fd5972a2502279b870a"
        ),
    },
    {
        "id": "e482a568-3a00-5e77-b668-ca19ba42fbaa",
        "version_id": "10ba0a01-7474-50d4-9cd8-06d027ca85af",
        "key": "relocate",
        "title": "Переставить байки",
        "task_type": "relocate",
        "task_title": "Переставить байки на точке",
        "details": (
            "Переместить байки по указанному адресу и убедиться, что они не "
            "мешают проходу."
        ),
        "reward": 100,
        "mode": "open",
        "evidence_policy": "photo_required",
        "max_participants": 1,
        "budget_cap": 100,
        "content_hash": (
            "56fc16f014d13187aab36bfd78662ab05f8820bce47e6ef02c57abc6547749cd"
        ),
    },
    {
        "id": "cf1573ed-f6ce-58f5-a18f-d3bf239c4b9b",
        "version_id": "cc48e39d-6578-5793-a383-879bc7913193",
        "key": "charge",
        "title": "Заменить батареи",
        "task_type": "charge",
        "task_title": "Заменить батареи в байках",
        "details": (
            "Заменить разряженные батареи и проверить, что байки снова "
            "доступны для поездки."
        ),
        "reward": 120,
        "mode": "open",
        "evidence_policy": "photo_required",
        "max_participants": 1,
        "budget_cap": 120,
        "content_hash": (
            "ada4d56af8db5c3ee83f8f49aeaefdae9736fd09dfc8eb01e3be7503b6897c8e"
        ),
    },
)

SYSTEM_TEMPLATE_SEED_AT = "2026-07-28T00:00:00+00:00"
