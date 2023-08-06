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


router = routers.DefaultRouter()


only_fields = openapi.Parameter('fields', openapi.IN_QUERY, description="Name of the fields to be retrieved", type=openapi.TYPE_STRING)
choices_field = openapi.Parameter('choices_field', openapi.IN_QUERY, description="Name of the field from which the choices will be displayed.", type=openapi.TYPE_STRING)
choices_search = openapi.Parameter('choices_search', openapi.IN_QUERY, description="Term to be used in the choices search.", type=openapi.TYPE_STRING)

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

    def remove_fields(self):
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
        return {attr: getattr(value, attr) for attr in self.attrs}

    def to_internal_value(self, data):
        return {attr: data[attr] for attr in self.attrs}


class Somar(serializers.Serializer):
    a = serializers.IntegerField()
    b = serializers.IntegerField()

    def submit(self):
        if self.is_valid(raise_exception=True):
            return dict(soma=self.data['a'] + self.data['b'])


def generic_search(qs, term):
    orm_lookups = [field.name for field in qs.model._meta.get_fields() if isinstance(field, models.CharField)] or ['id']
    search_terms = term.split(' ') if term else []
    conditions = []
    for search_term in search_terms:
        queries = [
            models.Q(**{orm_lookup: search_term})
            for orm_lookup in orm_lookups
        ]
        conditions.append(reduce(operator.or_, queries))
    return qs.filter(reduce(operator.and_, conditions)) if conditions else qs


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
        elif self.action == 'somar':
            return Somar
        return self.get_create_serializer_class()

    def get_list_serializer_class(self):
        fieldsets = self.view_fieldsets
        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                if self.list_fields:
                    fields = self.list_fields
                else:
                    exclude = ()

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.remove_fields()

        return Serializer

    def get_create_serializer_class(self):
        class Serializer(serializers.ModelSerializer):

            class Meta:
                model = self.model
                exclude = ()

        return Serializer

    def get_retrieve_serializer_class(self):
        fieldsets = self.view_fieldsets
        class Serializer(DynamicFieldsModelSerializer):
            class Meta:
                model = self.model
                exclude = ()

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.configure_fieldsets(fieldsets)
                self.remove_fields()

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
            return Response([{'id': obj.pk, 'text': str(obj)} for obj in generic_search(qs, term)[0:20]])
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(manual_parameters=[choices_field, choices_search])
    def create(self, request, *args, **kwargs):
        choices = request.query_params.get('choices_field')
        if choices:
            term = request.query_params.get('choices_search')
            qs = getattr(self.model, 'content_type').field.related_model.objects.all()
            return Response([{'id': obj.pk, 'text': str(obj)} for obj in generic_search(qs, term)[0:20]])
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

    @action(detail=False, methods=["post"], url_path=r'somar')
    def somar(self, request):
        return Response(Somar(data=request.data).submit(), status=status.HTTP_200_OK)



def model_view_set_factory(model_name, filters=(), search=(), fieldsets=(), ordering=(), l2=()):
    class ViewSet(ModelViewSet):
        view_fieldsets = fieldsets
        model = apps.get_model(model_name)
        filterset_fields = filters
        if search:
            search_fields = [name.strip() for name in search.split(',')]
        if ordering:
            ordering_fields = [name.strip() for name in ordering.split(',')]
        list_fields = [name.strip() for name in l2.split(',')] if l2 else ()
    return ViewSet


for k, v in yaml.safe_load(open('api.yml')).items():
    name = k.split('.')[-1]
    router.register(v.get('prefix'), model_view_set_factory(
        k,
        filters=v.get('filters', ()),
        search=v.get('search', ()),
        fieldsets=v.get('fieldsets', ()),
        ordering=v.get('ordering', ()),
        l2=v.get('list', ()),
    ), name)

