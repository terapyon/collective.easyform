from Acquisition import aq_parent, aq_inner
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from ZPublisher.BaseRequest import DefaultPublishTraverse
from plone.autoform.form import AutoExtensibleForm
from plone.memoize.instance import memoize
from plone.schemaeditor.browser.field.traversal import FieldContext
from plone.schemaeditor.browser.schema.add_field import FieldAddForm
from plone.schemaeditor.browser.schema.listing import SchemaListing, SchemaListingPage
from plone.schemaeditor.browser.schema.traversal import SchemaContext
from plone.schemaeditor.interfaces import IFieldEditFormSchema, IFieldEditorExtender
from plone.schemaeditor.utils import SchemaModifiedEvent
from plone.z3cform import layout
from plone.z3cform.crud import crud
from z3c.form import button, form, field
from zope.cachedescriptors.property import Lazy as lazy_property
from zope.component import queryUtility, getAdapters
from zope.event import notify
from zope.interface import implements
from collective.formulator.api import get_actions, get_fields, get_context
from collective.formulator import formulatorMessageFactory as _
from collective.formulator.interfaces import (
    IActionContext,
    IActionEditForm,
    IActionFactory,
    IFormulatorActionsContext,
    INewAction,
    IExtraData,
    ISaveData,
)
from zope.schema import getFieldsInOrder


class SavedDataView(BrowserView):

    def items(self):
        return [
            (name, action.__doc__)
            for name, action in getFieldsInOrder(get_actions(self.context))
            if ISaveData.providedBy(action)
        ]


class SavedDataForm(crud.CrudForm):
    template = ViewPageTemplateFile('saveddata_form.pt')
    addform_factory = crud.NullForm

    @property
    def field(self):
        return self.context.field

    @property
    def name(self):
        return self.field.__name__

    @property
    def formulator(self):
        return get_context(self.field)

    @property
    def storage(self):
        return self.field._storage

    def description(self):
        return _(u"${items} input(s) saved", mapping={'items': len(self.storage)})

    @property
    def update_schema(self):
        fields = field.Fields(get_fields(self.formulator))
        showFields = getattr(self.field, 'showFields', [])
        if showFields:
            fields = fields.select(*showFields)
        return fields

    @property
    def view_schema(self):
        fields = field.Fields(IExtraData)
        ExtraData = self.field.ExtraData
        if ExtraData:
            fields = fields.select(*ExtraData)
        return fields

    def get_items(self):
        return self.storage.items()

    # def add(self, data):
        #storage = self.context._inputStorage

    def before_update(self, item, data):
        id = item['id']
        item.update(data)
        self.storage[id] = item
        #sdata = self.storage[id]
        # sdata.update(data)
        #self.storage[id] = sdata

    def remove(self, (id, item)):
        del self.storage[id]

    @button.buttonAndHandler(_(u'Download'), name='download')
    def handleDownload(self, action):
        filename = '%s.csv' % self.name
        self.request.response.setHeader(
            "Content-Disposition", "attachment; filename=\"%s\"" % filename)
        self.request.response.setHeader(
            "Content-Type", 'text/comma-separated-values')
        self.request.response.write(self.field.download_csv())

    @button.buttonAndHandler(_(u'Clear all'), name='clearall')
    def handleClearAll(self, action):
        self.storage.clear()

ActionSavedDataView = layout.wrap_form(SavedDataForm)


class ActionContext(FieldContext):

    """ wrapper for published zope 3 schema fields
    """
    implements(IActionContext)

    def publishTraverse(self, request, name):
        """ It's not valid to traverse to anything below a field context.
        """
        # hack to make inline validation work
        # (plone.app.z3cform doesn't know the form is the default view)
        if name == self.__name__:
            return ActionEditView(self, request).__of__(self)

        return DefaultPublishTraverse(self, request).publishTraverse(request, name)


class FormulatorActionsView(SchemaContext):
    implements(IFormulatorActionsContext)

    schema = None

    def __init__(self, context, request):
        self.schema = get_actions(context)
        super(FormulatorActionsView, self).__init__(
            self.schema,
            request,
            name='actions'
        )

    def publishTraverse(self, request, name):
        """ Look up the field whose name matches the next URL path element, and wrap it.
        """
        try:
            return ActionContext(self.schema[name], self.request).__of__(self)
        except KeyError:
            return DefaultPublishTraverse(self, request).publishTraverse(request, name)

    def browserDefault(self, request):
        """ If not traversing through the schema to a field, show the SchemaListingPage.
        """
        return self, ('@@actions',)


class FormulatorActionsListing(SchemaListing):
    template = ViewPageTemplateFile('actions_listing.pt')

    @memoize
    def _field_factory(self, field):
        field_identifier = u'%s.%s' % (
            field.__module__, field.__class__.__name__)
        return queryUtility(IActionFactory, name=field_identifier)

    @button.buttonAndHandler(_(u'Save'))
    def handleSaveDefaults(self, action):
        data, errors = self.extractData()
        if errors:
            self.status = self.formErrorsMessage
            return

        for fname, value in data.items():
            self.context.schema[fname].required = value
        notify(SchemaModifiedEvent(self.context))

        # update widgets to take the new defaults into account
        self.updateWidgets()
        self.request.response.redirect(self.context.absolute_url())


class FormulatorActionsListingPage(SchemaListingPage):

    """ Form wrapper so we can get a form with layout.

        We define an explicit subclass rather than using the wrap_form method
        from plone.z3cform.layout so that we can inject the schema name into
        the form label.
    """
    form = FormulatorActionsListing
    index = ViewPageTemplateFile("model_listing.pt")


class ActionAddForm(FieldAddForm):

    fields = field.Fields(INewAction)
    label = _("Add new action")


ActionAddFormPage = layout.wrap_form(ActionAddForm)


class ActionEditForm(AutoExtensibleForm, form.EditForm):
    implements(IActionEditForm)

    def __init__(self, context, request):
        super(form.EditForm, self).__init__(context, request)
        self.field = context.field

    def getContent(self):
        return self.field

    @lazy_property
    def schema(self):
        return IFieldEditFormSchema(self.field)

    @lazy_property
    def additionalSchemata(self):
        schema_context = self.context.aq_parent
        return [v for k, v in getAdapters((schema_context, self.field), IFieldEditorExtender)]

    @button.buttonAndHandler(_(u'Save'), name='save')
    def handleSave(self, action):
        data, errors = self.extractData()
        if errors:
            self.status = self.formErrorsMessage
            return

        changes = self.applyChanges(data)

        if changes:
            self.status = self.successMessage
        else:
            self.status = self.noChangesMessage

        notify(SchemaModifiedEvent(self.context.aq_parent))
        self.redirectToParent()

    @button.buttonAndHandler(_(u'Cancel'), name='cancel')
    def handleCancel(self, action):
        self.redirectToParent()

    def redirectToParent(self):
        parent = aq_parent(aq_inner(self.context))
        url = parent.absolute_url()
        self.request.response.redirect(url)


class ActionEditView(layout.FormWrapper):
    form = ActionEditForm

    def __init__(self, context, request):
        super(ActionEditView, self).__init__(context, request)
        self.field = context.field

    @lazy_property
    def label(self):
        return _(u"Edit Action '${fieldname}'", mapping={'fieldname': self.field.__name__})
