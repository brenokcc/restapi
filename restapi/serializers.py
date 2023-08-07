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
from rest_framework.compat import coreapi, coreschema
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema

ACTIONS = {}

router = routers.DefaultRouter()


only_fields = openapi.Parameter('fields', openapi.IN_QUERY, description="Name of the fields to be retrieved", type=openapi.TYPE_STRING)
choices_field = openapi.Parameter('choices_field', openapi.IN_QUERY, description="Name of the field from which the choices will be displayed.", type=openapi.TYPE_STRING)
choices_search = openapi.Parameter('choices_search', openapi.IN_QUERY, description="Term to be used in the choices search.", type=openapi.TYPE_STRING)
id_parameter = openapi.Parameter('id', openapi.IN_PATH, description="The id of the object.", type=openapi.TYPE_INTEGER)

class DynamicFieldsModelSerializer(serializers.ModelSerializer):

    def __init__(self, *args, **kwargs):
        super(DynamicFieldsModelSerializer, self).__init__(*args, **kwargs)

    def configure_fieldsets(self, fieldsets):
        for k in fieldsets:
            v = fieldsets[k]
            help_text = 'Returns {}'.format(v)
            self.fields[k] = FieldsetField(source='*', names=v, help_text=help_text)
            for name in v:
                self.fields.pop(name, None)

    def remove_unrequested_fields(self):
        fields = self.context['request'].query_params.get('fields')
        if fields:
            fields = fields.split(',')
            allowed = set(name.strip() for name in fields)
            existing = set(self.fields.keys())
            for field_name in existing - allowed:
                self.fields.pop(field_name)


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


class ActionMetaclass(serializers.SerializerMetaclass):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        ACTIONS[name] = cls
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
            return ACTIONS[self.action_serializers[self.action]]
        return self.get_create_serializer_class()

    def get_list_serializer_class(self):
        if self.list_display:
            list_display = [name for name in self.list_display if name not in self.object_fieldsets]
            fieldsets = {k:v for k, v in self.object_fieldsets.items() if k in self.list_display}
        else:
            list_display = '__all__'
            fieldsets = self.object_fieldsets
        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                fields = list_display

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.configure_fieldsets(fieldsets)
                self.remove_unrequested_fields()

        return Serializer

    def get_create_serializer_class(self):
        class Serializer(serializers.ModelSerializer):

            class Meta:
                model = self.model
                exclude = ()

        return Serializer

    def get_retrieve_serializer_class(self):
        fields = []
        fieldsets = {}
        if self.view_display:
            for k in self.view_display:
                if k in self.object_fieldsets:
                    fieldsets[k] = self.object_fieldsets[k]
                    fields.extend(self.object_fieldsets[k])
                else:
                    fields.append(k)
        else:
            fieldsets.update(self.object_fieldsets)
        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                exclude = ()

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.configure_fieldsets(fieldsets)
                self.remove_unrequested_fields()
                if fields:
                    for k in list(self.fields.keys()):
                        if k not in fields:
                            self.fields.pop(k)

        return Serializer

    @swagger_auto_schema(manual_parameters=[only_fields])
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
        choices = request.query_params.get('choices_field')
        if choices:
            term = request.query_params.get('choices_search')
            qs = getattr(self.model, 'content_type').field.related_model.objects.all()
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
        print(self.source)
        return dict(soma=self.data['a'] + self.data['b'])

class RealizarSubtracao(Action):
    u = serializers.PrimaryKeyRelatedField(queryset=apps.get_model('auth.user').objects, label='User', initial=1)
    a = serializers.IntegerField()
    b = serializers.IntegerField()

    def submit(self):
        print(self.source)
        return dict(subtracao=self.data['a'] - self.data['b'])


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

