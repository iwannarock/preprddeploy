from django.contrib import admin
from basicservice.models import *
# Register your models here.

models = [BasicServiceDeployInfo, BasicServiceIps]
for model in models:
    admin.site.register(model)
