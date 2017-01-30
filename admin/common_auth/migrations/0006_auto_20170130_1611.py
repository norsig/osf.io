# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2017-01-30 22:11
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('common_auth', '0005_auto_20170111_1513'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='adminprofile',
            options={'permissions': (('mark_spam', 'Can mark comments, projects and registrations as spam'), ('view_spam', 'Can view nodes, comments, and projects marked as spam'), ('view_metrics', 'Can view metrics on the OSF Admin app'), ('view_prereg', 'Can view entries for the preregistration chellenge on the admin'), ('administer_prereg', 'Can update, comment on, and approve entries to the prereg challenge'))},
        ),
    ]
