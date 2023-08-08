import re
import yaml
import operator
from functools import reduce
from django.apps import apps
from rest_framework import serializers, viewsets
from rest_framework import routers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from rest_framework import filters
from django.db import models
from django.contrib.auth.models import User
from rest_framework.compat import coreapi, coreschema
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework.pagination import LimitOffsetPagination, PageNumberPagination
from rest_framework.relations import ManyRelatedField, MANY_RELATION_KWARGS
from django_filters.rest_framework import DjangoFilterBackend
from pathlib import Path
from django.utils.autoreload import autoreload_started

ACTIONS = {}

router = routers.DefaultRouter()


only_fields = openapi.Parameter('only', openapi.IN_QUERY, description="Name of the fields to be retrieved.", type=openapi.TYPE_STRING)
limit_field = openapi.Parameter('limit', openapi.IN_QUERY, description="Number of results to return per page.", type=openapi.TYPE_STRING)
offset_field = openapi.Parameter('offset', openapi.IN_QUERY, description="The initial index from which to return the results.", type=openapi.TYPE_STRING)
choices_field = openapi.Parameter('choices_field', openapi.IN_QUERY, description="Name of the field from which the choices will be displayed.", type=openapi.TYPE_STRING)
choices_search = openapi.Parameter('choices_search', openapi.IN_QUERY, description="Term to be used in the choices search.", type=openapi.TYPE_STRING)
id_parameter = openapi.Parameter('id', openapi.IN_PATH, description="The id of the object.", type=openapi.TYPE_INTEGER)


class MethodField(serializers.Field):

    def __init__(self, *args, method_name=None, **kwargs):
        self.method_name = method_name
        super().__init__(*args, **kwargs)

    def to_representation(self, instance):
        paginator = LimitOffsetPagination()
        value = getattr(instance, self.method_name)()
        if isinstance(value, models.Manager) or isinstance(value, models.QuerySet):
            if isinstance(value, models.Manager):
                value = value.all()
            queryset = paginator.paginate_queryset(value, self.context['request'], self.context['view'])
            data = [{'id': value.pk, 'text': str(value)} for value in queryset]
            return paginator.get_paginated_response(data).data
        elif isinstance(value, dict) or isinstance(value, list):
            return value
        elif isinstance(value, models.Model):
            return {'id': value.id, 'text':str(value)}
        else:
            return dict(value=value)


class PaginableManyRelatedField(ManyRelatedField):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paginator = LimitOffsetPagination()

    def get_attribute(self, instance):
        return self.paginator.paginate_queryset(super().get_attribute(instance), self.context['request'], self.context['view'])

    def to_representation(self, value):
        data = super().to_representation(value)
        return self.paginator.get_paginated_response(data).data

class RelationSerializer(serializers.RelatedField):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        return  {'id': value.pk, 'text': str(value)}

    @classmethod
    def many_init(cls, *args, **kwargs):
        list_kwargs = {'child_relation': cls(*args, **kwargs)}
        for key in kwargs:
            if key in MANY_RELATION_KWARGS:
                list_kwargs[key] = kwargs[key]
        return PaginableManyRelatedField(**list_kwargs)


class DynamicFieldsModelSerializer(serializers.ModelSerializer):
    serializer_related_field = RelationSerializer

    def __init__(self, *args, **kwargs):
        super(DynamicFieldsModelSerializer, self).__init__(*args, **kwargs)
        self.remove_unrequested_fields()

    def to_representation(self, value):
        representation = super().to_representation(value)
        return representation

    def build_unknown_field(self, field_name, model_class):
        method_name = 'get_{}'.format(field_name)
        if method_name in self.context['view'].view_methods:
            return MethodField, dict(source='*', method_name=method_name)
        if field_name in self.context['view'].object_fieldsets:
            names = self.context['view'].object_fieldsets[field_name]
            return FieldsetField, dict(source='*', names=names, help_text='Returns {}'.format(names))
        if field_name in self.context['view'].action_serializers:
            serializer_class = ACTIONS[self.context['view'].action_serializers[field_name]]
            return ActionField, dict(source='*', serializer_class=serializer_class)
        if field_name in ACTIONS:
            return ActionField, dict(source='*', serializer_class=ACTIONS[field_name])
        super().build_unknown_field(field_name, model_class)

    def remove_unrequested_fields(self):
        names = self.context['request'].query_params.get('only')
        if names:
            allowed = set(name.strip() for name in names.split(','))
            existing = set(self.fields.keys())
            for field_name in existing - allowed:
                self.fields.pop(field_name)


class ActionField(serializers.DictField):

    def __init__(self, serializer_class, *args, **kwargs):
        self.serializer_class = serializer_class
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        serializer = self.serializer_class(data={})
        if serializer.is_valid():
            return serializer.submit()
        return None

    def to_internal_value(self, data):
        return {}


class FieldsetField(serializers.DictField):

    def __init__(self, *args, **kwargs):
        self.attrs = []
        for attr in kwargs.pop('names').split(','):
            self.attrs.append(attr.strip())
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        return {attr: getattr(value, attr)() if callable(getattr(value, attr)) else getattr(value, attr) for attr in self.attrs}

    def to_internal_value(self, data):
        return {attr: data[attr] for attr in self.attrs}


def generic_search(qs, term):
    orm_lookups = [field.name for field in qs.model._meta.get_fields() if isinstance(field, models.CharField)] or ['id']
    search_terms = term.split(' ') if term else []
    conditions = []
    for search_term in search_terms:
        queries = [
            models.Q(**{f'{orm_lookup}__icontains': search_term})
            for orm_lookup in orm_lookups
        ]
        conditions.append(reduce(operator.or_, queries))
    return qs.filter(reduce(operator.and_, conditions)) if conditions else qs

def as_choices(qs, limit=20):
    return [{'id': obj.pk, 'text': str(obj)} for obj in qs[0:limit]]


class ChoiceFilter(filters.BaseFilterBackend):

    def filter_queryset(self, request, queryset, view):
        return queryset

    def get_schema_fields(self, view):
        assert coreapi is not None, 'coreapi must be installed to use `get_schema_fields()`'
        assert coreschema is not None, 'coreschema must be installed to use `get_schema_fields()`'
        return [
            coreapi.Field(
                name='choices',
                required=False,
                location='query',
                schema=coreschema.String(
                    title='Name of the field',
                    description='Name of the field to display choices'
                )
            )
        ]

    def get_schema_operation_parameters(self, view):
        return [
            {
                'name': 'choices',
                'required': False,
                'in': 'query',
                'description': 'Name of the field',
                'schema': {
                    'type': 'string',
                },
            },
        ]


def to_snake_case(name):
    return name if name.islower() else re.sub(r'(?<!^)(?=[A-Z0-9])', '_', name).lower()


class ActionMetaclass(serializers.SerializerMetaclass):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        ACTIONS[name] = cls
        ACTIONS[to_snake_case(name)] = cls
        return cls


class Action(serializers.Serializer, metaclass=ActionMetaclass):

    def __init__(self, *args, **kwargs):
        self.source = None
        super().__init__(*args, **kwargs)

    def has_permission(self):
        return False



class ModelViewSet(viewsets.ModelViewSet):
    # filter_backends = [ChoiceFilter]

    def __init__(self, *args, **kwargs):
        self.queryset = self.get_queryset()
        self.fieldsets = kwargs.pop('fieldsets', ())
        super().__init__(*args, **kwargs)

    def get_queryset(self):
        return self.model.objects.all()

    def get_serializer_class(self):
        if self.action == 'list':
            return self.get_list_serializer_class()
        if self.action == 'retrieve':
            return self.get_retrieve_serializer_class()
        if self.action == 'update':
            return self.get_update_serializer_class()
        if self.action == 'partial_update':
            return self.get_partial_update_serializer_class()
        elif self.action in self.action_serializers:
            return ACTIONS[self.action_serializers.get(self.action, self.action)]
        return self.get_create_serializer_class()

    def get_list_serializer_class(self):

        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                fields = self.list_display

        return Serializer

    def get_create_serializer_class(self):
        class Serializer(serializers.ModelSerializer):

            class Meta:
                model = self.model
                exclude = ()

        return Serializer

    def get_retrieve_serializer_class(self):

        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                fields = self.view_display if self.view_display else '__all__'

        return Serializer

    @swagger_auto_schema(manual_parameters=[only_fields, limit_field, offset_field])
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(manual_parameters=[choices_field, choices_search, only_fields])
    def list(self, request, *args, **kwargs):
        choices = request.query_params.get('choices_field')
        if choices:
            term = request.query_params.get('choices_search')
            qs = getattr(self.model, choices).field.related_model.objects.all()
            qs = qs.filter(id__in=self.filter_queryset(self.get_queryset()).values_list(choices, flat=True))
            return Response(as_choices(generic_search(qs, term)))
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(manual_parameters=[choices_field, choices_search])
    def create(self, request, *args, **kwargs):
        field_name = request.query_params.get('choices_field')
        if field_name:
            term = request.query_params.get('choices_search')
            qs = getattr(self.model, field_name).field.related_model.objects.all()
            return Response(as_choices(generic_search(qs, term)))
        return super().create(request, *args, **kwargs)

    def get_update_serializer_class(self):
        class Serializer(serializers.ModelSerializer):
            class Meta:
                model = self.model
                exclude = ()
        return Serializer

    def get_partial_update_serializer_class(self):
        class Serializer(serializers.ModelSerializer):
            class Meta:
                model = self.model
                exclude = ()
        return Serializer

    @action(detail=False, methods=["get"], url_path=r'inativos')
    def inativos(self, request):
        qs = self.model.objects.filter(is_active=False)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

def str_to_list(s):
    return [name.strip() for name in s.split(',')] if s else []

def iter_to_list(i):
    return [o for o in i]

def model_view_set_factory(model_name, filters=(), search=(), ordering=(), fieldsets=(), _view_display=(), _list_display=(), _view_actions=(), _list_actions=()):
    class ViewSet(ModelViewSet):
        object_fieldsets = fieldsets
        model = apps.get_model(model_name)
        filterset_fields = str_to_list(filters)
        if search:
            search_fields = [name.strip() for name in search.split(',')]
        if ordering:
            ordering_fields = [name.strip() for name in ordering.split(',')]
        view_display = str_to_list(_view_display)
        list_display = str_to_list(_list_display)
        view_actions = iter_to_list(_view_actions)
        list_actions = iter_to_list(_list_actions)
        action_serializers = {k:_view_actions[k] for k in _view_actions} | {k:_list_actions[k] for k in _list_actions}
        view_methods = [name for name in (view_display+list_display) if name.startswith('get_')]
        view_display = [name[4:] if name.startswith('get_') else name for name in view_display]
        list_display = [name[4:] if name.startswith('get_') else name for name in list_display]

    actions = {}
    for d in (_view_actions, _list_actions):
        for k in d:
            actions[k] = d[k]

    for k in actions:
        function = create_action_func(apps.get_model(model_name), k, actions[k])
        manual_parameters = [choices_field, choices_search]
        if k in _view_actions:
            manual_parameters.append(id_parameter)
        swagger_auto_schema(manual_parameters=manual_parameters)(function)
        action(detail=k in _view_actions, methods=["post"], url_path=k, url_name=k, name=k)(function)
        setattr(ViewSet, k, function)
    return ViewSet


def create_action_func(model, func_name, serializer_name):
    def func(self, request, *args, **kwargs):
        serializer = ACTIONS[serializer_name](data=request.data)
        serializer.source = model.objects.get(pk=kwargs['pk']) if 'pk' in kwargs else model.objects
        choices = request.query_params.get('choices_field')
        if choices:
            term = request.query_params.get('choices_search')
            qs = serializer.fields[choices].queryset.all()
            return Response(as_choices(generic_search(qs, term)))
        if request.method.lower() == 'get':
            return Response(serializer.initial_data, status=status.HTTP_200_OK)
        if serializer.is_valid():
            return Response(serializer.submit(), status=status.HTTP_200_OK)
        else:
            return Response(dict(errors=serializer.errors), status=status.HTTP_200_OK)

    func.__name__ = func_name
    return func


class RealizarSoma(Action):
    u = serializers.PrimaryKeyRelatedField(queryset=apps.get_model('auth.user').objects, label='User', initial=1)
    a = serializers.IntegerField()
    b = serializers.IntegerField()

    def submit(self):
        return dict(soma=self.data['a'] + self.data['b'])

class RealizarSubtracao(Action):
    u = serializers.PrimaryKeyRelatedField(queryset=apps.get_model('auth.user').objects, label='User', initial=1)
    a = serializers.IntegerField()
    b = serializers.IntegerField()

    def submit(self):
        return dict(subtracao=self.data['a'] - self.data['b'])


class ExibirAlertas(Action):
    def submit(self):
        return {'a': 1}

class ExibirCartoes(Action):
    def submit(self):
        return {'b': 2}


specification = yaml.safe_load(open('api.yml'))
for k, v in specification.get('models').items():
    name = k.split('.')[-1]
    list_display = v.get('list', {}).get('fields', ())
    view_display = v.get('view', {}).get('fields', ())
    list_actions = v.get('list', {}).get('actions', ())
    view_actions = v.get('view', {}).get('actions', ())
    router.register(
        v.get('prefix'),
        model_view_set_factory(
            k,
            filters=v.get('filters', ()),
            search=v.get('search', ()),
            ordering=v.get('ordering', ()),
            fieldsets=v.get('fieldsets', ()),
            _view_display=view_display,
            _list_display=list_display,
            _view_actions=view_actions,
            _list_actions=list_actions,
        ),
        name
    )

def api_watchdog(sender, **kwargs):
    sender.extra_files.add(Path('api.yml'))
autoreload_started.connect(api_watchdog)

