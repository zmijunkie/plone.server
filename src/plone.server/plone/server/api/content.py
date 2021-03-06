# -*- coding: utf-8 -*-
from aiohttp.web_exceptions import HTTPMethodNotAllowed
from aiohttp.web_exceptions import HTTPNotFound
from aiohttp.web_exceptions import HTTPUnauthorized
from dateutil.tz import tzlocal
from plone.server import app_settings
from plone.server import configure
from plone.server import _
from plone.server.api.service import Service
from plone.server.browser import ErrorResponse
from plone.server.browser import Response
from plone.server.content import create_content_in_container
from plone.server.events import notify
from plone.server.events import ObjectFinallyCreatedEvent
from plone.server.events import ObjectFinallyDeletedEvent
from plone.server.events import ObjectFinallyModifiedEvent
from plone.server.events import ObjectFinallyVisitedEvent
from plone.server.events import ObjectPermissionsModifiedEvent
from plone.server.events import ObjectPermissionsViewEvent
from plone.server.exceptions import ConflictIdOnContainer
from plone.server.exceptions import PreconditionFailed
from plone.server.interfaces import IAbsoluteURL
from plone.server.interfaces import IResource
from plone.server.json.exceptions import DeserializationError
from plone.server.interfaces import IResourceDeserializeFromJson
from plone.server.interfaces import IResourceSerializeToJson
from plone.server.utils import get_authenticated_user_id
from plone.server.utils import iter_parents
from zope.component import getMultiAdapter
from zope.component import queryMultiAdapter

from zope.securitypolicy.interfaces import IPrincipalPermissionMap
from zope.securitypolicy.interfaces import IPrincipalRoleManager
from zope.securitypolicy.interfaces import IRolePermissionManager
from zope.securitypolicy.interfaces import IPrincipalRoleMap
from zope.securitypolicy.interfaces import IRolePermissionMap


_zone = tzlocal()


@configure.service(context=IResource, method='GET', permission='plone.ViewContent')
class DefaultGET(Service):
    async def __call__(self):
        serializer = getMultiAdapter(
            (self.context, self.request),
            IResourceSerializeToJson)
        result = serializer()
        await notify(ObjectFinallyVisitedEvent(self.context))
        return result


@configure.service(context=IResource, method='POST', permission='plone.AddContent')
class DefaultPOST(Service):

    async def __call__(self):
        """To create a content."""
        data = await self.get_data()
        type_ = data.get('@type', None)
        id_ = data.get('id', None)
        behaviors = data.get('@behaviors', None)

        if not type_:
            return ErrorResponse(
                'RequiredParam',
                _("Property '@type' is required"))

        # Generate a temporary id if the id is not given
        if not id_:
            new_id = None
        else:
            new_id = id_

        user = get_authenticated_user_id(self.request)
        # Create object
        try:
            obj = create_content_in_container(
                self.context, type_, new_id, id=new_id, creators=(user,),
                contributors=(user,))
        except PreconditionFailed as e:
            return ErrorResponse(
                'PreconditionFailed',
                str(e),
                status=412)
        except ConflictIdOnContainer as e:
            return ErrorResponse(
                'ConflictId',
                str(e),
                status=409)
        except ValueError as e:
            return ErrorResponse(
                'CreatingObject',
                str(e),
                status=400)

        for behavior in behaviors or ():
            obj.add_behavior(behavior)

        # Update fields
        deserializer = queryMultiAdapter((obj, self.request),
                                         IResourceDeserializeFromJson)
        if deserializer is None:
            return ErrorResponse(
                'DeserializationError',
                'Cannot deserialize type {}'.format(obj.portal_type),
                status=501)

        try:
            await deserializer(data, validate_all=True)
        except DeserializationError as e:
            return ErrorResponse(
                'DeserializationError',
                str(e),
                exc=e,
                status=400)

        # Local Roles assign owner as the creator user
        roleperm = IPrincipalRoleManager(obj)
        roleperm.assignRoleToPrincipal(
            'plone.Owner',
            user)

        await notify(ObjectFinallyCreatedEvent(obj))

        absolute_url = queryMultiAdapter((obj, self.request), IAbsoluteURL)

        headers = {
            'Access-Control-Expose-Headers': 'Location',
            'Location': absolute_url()
        }

        serializer = queryMultiAdapter(
            (obj, self.request),
            IResourceSerializeToJson
        )
        return Response(response=serializer(), headers=headers, status=201)


@configure.service(context=IResource, method='PUT', permission='plone.ModifyContent')
class DefaultPUT(Service):
    pass


@configure.service(context=IResource, method='PATCH', permission='plone.ModifyContent')
class DefaultPATCH(Service):
    async def __call__(self):
        data = await self.get_data()
        behaviors = data.get('@behaviors', None)
        for behavior in behaviors or ():
            self.context.add_behavior(behavior)

        deserializer = queryMultiAdapter((self.context, self.request),
                                         IResourceDeserializeFromJson)
        if deserializer is None:
            return ErrorResponse(
                'DeserializationError',
                'Cannot deserialize type {}'.format(self.context.portal_type),
                status=501)

        try:
            await deserializer(data)
        except DeserializationError as e:
            return ErrorResponse(
                'DeserializationError',
                str(e),
                status=400)

        await notify(ObjectFinallyModifiedEvent(self.context))

        return Response(response={}, status=204)


@configure.service(context=IResource, method='GET', permission='plone.SeePermissions',
                   name='@sharing')
async def sharing_get(context, request):
    roleperm = IRolePermissionMap(context)
    prinperm = IPrincipalPermissionMap(context)
    prinrole = IPrincipalRoleMap(context)
    result = {
        'local': {},
        'inherit': []
    }
    result['local']['role_permission'] = roleperm._byrow
    result['local']['principal_permission'] = prinperm._byrow
    result['local']['principal_role'] = prinrole._byrow
    for obj in iter_parents(context):
        roleperm = IRolePermissionMap(obj)
        prinperm = IPrincipalPermissionMap(obj)
        prinrole = IPrincipalRoleMap(obj)
        result['inherit'].append({
            '@id': IAbsoluteURL(obj, request)(),
            'role_permission': roleperm._byrow,
            'principal_permission': prinperm._byrow,
            'principal_role': prinrole._byrow,
        })
    await notify(ObjectPermissionsViewEvent(context))
    return result


@configure.service(context=IResource, method='POST', permission='plone.ChangePermissions',
                   name='@sharing')
async def sharing_post(context, request):
    data = await request.json()
    roleperm = IRolePermissionManager(context)
    prinrole = IPrincipalRoleManager(context)
    if 'prinrole' not in data and 'roleperm' not in data:
        raise AttributeError('prinrole or roleperm missing')

    if 'prinrole' in data:
        for user, roles in data['prinrole'].items():
            for role in roles:
                prinrole.assignRoleToPrincipal(role, user)

    if 'roleperm' in data:
        for role, perms in data['roleperm'].items():
            for perm in perms:
                roleperm.grantPermissionToRole(perm, role)
    await notify(ObjectPermissionsModifiedEvent(context))


@configure.service(context=IResource, method='DELETE', permission='plone.DeleteContent')
class DefaultDELETE(Service):

    async def __call__(self):
        content_id = self.context.id
        del self.context.__parent__[content_id]
        await notify(ObjectFinallyDeletedEvent(self.context))


@configure.service(context=IResource, method='OPTIONS', permission='plone.AccessPreflight')
class DefaultOPTIONS(Service):
    """Preflight view for Cors support on DX content."""

    def getRequestMethod(self):  # noqa
        """Get the requested method."""
        return self.request.headers.get(
            'Access-Control-Request-Method', None)

    async def preflight(self):
        """We need to check if there is cors enabled and is valid."""
        headers = {}

        if not app_settings['cors']:
            return {}

        origin = self.request.headers.get('Origin', None)
        if not origin:
            raise HTTPNotFound(text='Origin this header is mandatory')

        requested_method = self.getRequestMethod()
        if not requested_method:
            raise HTTPNotFound(
                text='Access-Control-Request-Method this header is mandatory')

        requested_headers = (
            self.request.headers.get('Access-Control-Request-Headers', ()))

        if requested_headers:
            requested_headers = map(str.strip, requested_headers.split(', '))

        requested_method = requested_method.upper()
        allowed_methods = app_settings['cors']['allow_methods']
        if requested_method not in allowed_methods:
            raise HTTPMethodNotAllowed(
                requested_method, allowed_methods,
                text='Access-Control-Request-Method Method not allowed')

        supported_headers = app_settings['cors']['allow_headers']
        if '*' not in supported_headers and requested_headers:
            supported_headers = [s.lower() for s in supported_headers]
            for h in requested_headers:
                if not h.lower() in supported_headers:
                    raise HTTPUnauthorized(
                        text='Access-Control-Request-Headers Header %s not allowed' % h)

        supported_headers = [] if supported_headers is None else supported_headers
        requested_headers = [] if requested_headers is None else requested_headers

        supported_headers = set(supported_headers) | set(requested_headers)

        headers['Access-Control-Allow-Headers'] = ','.join(
            supported_headers)
        headers['Access-Control-Allow-Methods'] = ','.join(
            app_settings['cors']['allow_methods'])
        headers['Access-Control-Max-Age'] = str(app_settings['cors']['max_age'])
        return headers

    async def render(self):
        """Need to be overwritten in case you implement OPTIONS."""
        return {}

    async def __call__(self):
        """Apply CORS on the OPTIONS view."""
        headers = await self.preflight()
        resp = await self.render()
        if isinstance(resp, Response):
            headers.update(resp.headers)
            resp.headers = headers
            return resp
        return Response(response=resp, headers=headers, status=200)
