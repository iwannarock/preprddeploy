# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('permission', '0003_auto_20170316_1953'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sitepage',
            name='name',
            field=models.CharField(max_length=50, choices=[(b'permission', '\u7528\u6237\u9a8c\u8bc1\u4e0e\u6743\u9650\u7ba1\u7406'), (b'module', '\u6a21\u5757\u7248\u672c\u7ba1\u7406'), (b'elb', '\u8d1f\u8f7d\u5747\u8861\u5668\u7ba1\u7406'), (b'basic', '\u57fa\u7840\u670d\u52a1\u7ba1\u7406'), (b'uploader', '\u5305\u4e0a\u4f20')]),
        ),
    ]
