# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-09-12 13:44
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('frontend', '0057_auto_20190904_1924'),
    ]

    operations = [
        migrations.AddField(
            model_name='measure',
            name='analyse_url',
            field=models.CharField(max_length=1000, null=True),
        ),
    ]
