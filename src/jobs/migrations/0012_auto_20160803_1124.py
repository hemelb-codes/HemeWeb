# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-08-03 11:24
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0011_auto_20160728_2304'),
    ]

    operations = [
        migrations.AlterField(
            model_name='job',
            name='status',
            field=models.IntegerField(choices=[(1, b'Added'), (2, b'Queued'), (10, b'Running'), (6, b'Running postprocessing scripts'), (3, b'Done'), (0, b'Failed'), (4, b'Preprocessing'), (5, b'Previous Job on S3')], default=1),
        ),
    ]
