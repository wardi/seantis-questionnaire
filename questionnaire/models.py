from django.db import models
from transmeta import TransMeta
from django.utils.translation import ugettext_lazy as _
from questionnaire import QuestionChoices
import re
from utils import split_numal
from django.utils import simplejson as json
from parsers import parse_checks, ParseException
from django.conf import settings

_numre = re.compile("(\d+)([a-z]+)", re.I)


class Subject(models.Model):
    STATE_CHOICES = [
        ("active", _("Active")),
        ("inactive", _("Inactive")),
        # Can be changed from elsewhere with
        # Subject.STATE_CHOICES[:] = [ ('blah', 'Blah') ]
    ]
    state = models.CharField(max_length=16, default="inactive",
        choices = STATE_CHOICES, verbose_name=_('State'))
    surname = models.CharField(max_length=64, blank=True, null=True,
        verbose_name=_('Surname'))
    givenname = models.CharField(max_length=64, blank=True, null=True,
        verbose_name=_('Given name'))
    email = models.EmailField(null=True, blank=True, verbose_name=_('Email'))
    gender = models.CharField(max_length=8, default="unset", blank=True,
        verbose_name=_('Gender'),
        choices = ( ("unset", _("Unset")),
                    ("male", _("Male")),
                    ("female", _("Female")),
        )
    )
    nextrun = models.DateField(verbose_name=_('Next Run'), blank=True, null=True)
    formtype = models.CharField(max_length=16, default='email',
        verbose_name = _('Form Type'),
        choices = (
            ("email", _("Subject receives emails")),
            ("paperform", _("Subject is sent paper form"),))
    )
    language = models.CharField(max_length=2, default=settings.LANGUAGE_CODE,
        verbose_name = _('Language'), choices = settings.LANGUAGES)

    def __unicode__(self):
        return u'%s, %s (%s)' % (self.surname, self.givenname, self.email)

    def next_runid(self):
        "Return the string form of the runid for the upcoming run"
        return str(self.nextrun.year)

    def last_run(self):
        "Returns the last completed run or None"
        try:
            query = RunInfoHistory.objects.filter(subject=self)
            return query.order_by('-completed')[0]
        except IndexError:
            return None

    def history(self):
        return RunInfoHistory.objects.filter(subject=self).order_by('runid')

    def pending(self):
        return RunInfo.objects.filter(subject=self).order_by('runid')


class QuestionnaireManager(models.Manager):
    def create_from_json_data(self, data, schema_version=1):
        """
        Create a new Questionnaire including all QuestionSets, Questions and
        Choices using previously exported questionnaire data.
        """
        support_only_versions(schema_version, (1,))
        q = self.create(**dict_filter_keys(data, ('name', 'redirect_url')))
        for qus_data in data['questionsets']:
            QuestionSet.objects.create_from_json_data(
                qus_data, schema_version, questionnaire=q,)
        return q


class Questionnaire(models.Model):
    objects = QuestionnaireManager()
    name = models.CharField(max_length=128)
    redirect_url = models.CharField(max_length=128, help_text="URL to redirect to when Questionnaire is complete. Macros: $SUBJECTID, $RUNID, $LANG", default="/static/complete.html")

    def __unicode__(self):
        return self.name

    def questionsets(self):
        if not hasattr(self, "__qscache"):
            self.__qscache = \
              QuestionSet.objects.filter(questionnaire=self).order_by('sortid')
        return self.__qscache

    class Meta:
        permissions = (
            ("export", "Can export questionnaire answers"),
            ("management", "Management Tools")
        )

    def dump_json_data(self, schema_version=1):
        support_only_versions(schema_version, (1,))
        return dict(
            questionsets=[qus.dump_json_data(schema_version)
                for qus in self.questionsets()],
            **attrs_to_dict(self, ('name', 'redirect_url')))


class QuestionSetManager(models.Manager):
    def create_from_json_data(self, data, schema_version=1, questionnaire=None):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text'
        qus = self.create(
            questionnaire=questionnaire,
            **dict_filter_keys(data, ('sortid', 'heading', 'checks', 'text')))
        for qu_data in data['questions']:
            Question.objects.create_from_json_data(
                qu_data, schema_version, questionset=qus)
        return qus


class QuestionSet(models.Model):
    __metaclass__ = TransMeta
    objects = QuestionSetManager()

    "Which questions to display on a question page"
    questionnaire = models.ForeignKey(Questionnaire)
    sortid = models.IntegerField() # used to decide which order to display in
    heading = models.CharField(max_length=64)
    checks = models.CharField(max_length=128, blank=True,
        help_text = """Current options are 'femaleonly' or 'maleonly' and shownif="QuestionNumber,Answer" which takes the same format as <tt>requiredif</tt> for questions.""")
    text = models.TextField(help_text="This is interpreted as Textile: <a href='http://hobix.com/textile/quick.html'>http://hobix.com/textile/quick.html</a>")

    def questions(self):
        if not hasattr(self, "__qcache"):
            self.__qcache = list(Question.objects.filter(questionset=self).order_by('number'))
            self.__qcache.sort()
        return self.__qcache

    def next(self):
        qs = self.questionnaire.questionsets()
        retnext = False
        for q in qs:
            if retnext:
                return q
            if q == self:
                retnext = True
        return None

    def prev(self):
        qs = self.questionnaire.questionsets()
        last = None
        for q in qs:
            if q == self:
                return last
            last = q

    def is_last(self):
        try:
            return self.questionnaire.questionsets()[-1] == self
        except NameError:
            # should only occur if not yet saved
            return True

    def is_first(self):
        try:
            return self.questionnaire.questionsets()[0] == self
        except NameError:
            # should only occur if not yet saved
            return True

    def __unicode__(self):
        return u'%s: %s' % (self.questionnaire.name, self.heading)

    class Meta:
        translate = ('text',)

    def dump_json_data(self, schema_version=1):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text'
        return dict(
            questions=[qu.dump_json_data(schema_version)
                for qu in self.questions()],
            **attrs_to_dict(self, ('sortid', 'heading', 'checks', 'text')))


class RunInfo(models.Model):
    "Store the active/waiting questionnaire runs here"
    subject = models.ForeignKey(Subject)
    random = models.CharField(max_length=32) # probably a randomized md5sum
    runid = models.CharField(max_length=32)
    # questionset should be set to the first QuestionSet initially, and to null on completion
    # ... although the RunInfo entry should be deleted then anyway.
    questionset = models.ForeignKey(QuestionSet, blank=True, null=True) # or straight int?
    emailcount = models.IntegerField(default=0)

    created = models.DateTimeField(auto_now_add=True)
    emailsent = models.DateTimeField(null=True, blank=True)

    lastemailerror = models.CharField(max_length=64, null=True, blank=True)

    state = models.CharField(max_length=16, null=True, blank=True)
    cookies = models.TextField(null=True, blank=True)

    tags = models.TextField(
            blank=True,
            help_text=u"Tags active on this run, separated by commas"
        )

    skipped = models.TextField(
            blank=True,
            help_text=u"A comma sepearted list of questions to skip"
        )

    def save(self):
        self.random = (self.random or '').lower()
        super(RunInfo, self).save()

    def set_cookie(self, key, value):
        "runinfo.set_cookie(key, value). If value is None, delete cookie"
        key = key.lower().strip()
        cookies = self.get_cookiedict()
        if type(value) not in (int, float, str, unicode, type(None)):
            raise Exception("Can only store cookies of type integer or string")
        if value is None:
            if key in cookies:
                del cookies[key]
        else:
            if type(value) in ('int', 'float'):
                value=str(value)
            cookies[key] = value
        cstr = json.dumps(cookies)
        self.cookies=cstr
        self.save()
        self.__cookiecache = cookies

    def get_cookie(self, key, default=None):
        if not self.cookies:
            return default
        d = self.get_cookiedict()
        return d.get(key.lower().strip(), default)

    def get_cookiedict(self):
        if not self.cookies:
            return {}
        if not hasattr(self, '__cookiecache'):
            self.__cookiecache = json.loads(self.cookies)
        return self.__cookiecache

    def __unicode__(self):
        return "%s: %s, %s" % (self.runid, self.subject.surname, self.subject.givenname)

    class Meta:
        verbose_name_plural = 'Run Info'


class RunInfoHistory(models.Model):
    subject = models.ForeignKey(Subject)
    runid = models.CharField(max_length=32)
    completed = models.DateField()
    tags = models.TextField(
            blank=True,
            help_text=u"Tags used on this run, separated by commas"
        )
    skipped = models.TextField(
            blank=True,
            help_text=u"A comma sepearted list of questions skipped by this run"
        )
    questionnaire = models.ForeignKey(Questionnaire)

    def __unicode__(self):
        return "%s: %s on %s" % (self.runid, self.subject, self.completed)

    def answers(self):
        "Returns the query for the answers."
        return Answer.objects.filter(subject=self.subject, runid=self.runid)

    class Meta:
        verbose_name_plural = 'Run Info History'


class QuestionManager(models.Manager):
    def create_from_json_data(self, data, schema_version=1, questionset=None):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text', 'extra'
        qu = self.create(
            questionset=questionset,
            **dict_filter_keys(data,
                ('number', 'text', 'type', 'extra', 'checks')))
        for ch_data in data['choices']:
            Choice.objects.create_from_json_data(
                ch_data, schema_version, question=qu)
        return qu


class Question(models.Model):
    __metaclass__ = TransMeta
    objects = QuestionManager()

    questionset = models.ForeignKey(QuestionSet)
    number = models.CharField(max_length=8, help_text=
        "eg. <tt>1</tt>, <tt>2a</tt>, <tt>2b</tt>, <tt>3c</tt><br /> "
        "Number is also used for ordering questions.")
    text = models.TextField(blank=True)
    type = models.CharField(u"Type of question", max_length=32,
        choices = QuestionChoices,
        help_text = u"Determines the means of answering the question. " \
        "An open question gives the user a single-line textfield, " \
        "multiple-choice gives the user a number of choices he/she can " \
        "choose from. If a question is multiple-choice, enter the choices " \
        "this user can choose from below'.")
    extra = models.CharField(u"Extra information", max_length=128, blank=True, null=True, help_text=u"Extra information (use  on question type)")
    checks = models.CharField(u"Additional checks", max_length=128, blank=True,
        null=True, help_text="Additional checks to be performed for this "
        "value (space separated)  <br /><br />"
        "For text fields, <tt>required</tt> is a valid check.<br />"
        "For yes/no choice, <tt>required</tt>, <tt>required-yes</tt>, "
        "and <tt>required-no</tt> are valid.<br /><br />"
        "If this question is required only if another question's answer is "
        'something specific, use <tt>requiredif="QuestionNumber,Value"</tt> '
        'or <tt>requiredif="QuestionNumber,!Value"</tt> for anything but '
        "a specific value.  "
        "You may also combine tests appearing in <tt>requiredif</tt> "
        "by joining them with the words <tt>and</tt> or <tt>or</tt>, "
        'eg. <tt>requiredif="Q1,A or Q2,B"</tt>')
    footer = models.TextField(u"Footer", help_text="Footer rendered below the question interpreted as textile", blank=True)


    def questionnaire(self):
        return self.questionset.questionnaire

    def getcheckdict(self):
        """getcheckdict returns a dictionary of the values in self.checks"""
        if(hasattr(self, '__checkdict_cached')):
            return self.__checkdict_cached
        try:
            self.__checkdict_cached = d = parse_checks(self.sameas().checks or '')
        except ParseException:
            raise Exception("Error Parsing Checks for Question %s: %s" % (
                self.number, self.sameas().checks))
        return d

    def __unicode__(self):
        return u'{%s} (%s) %s' % (unicode(self.questionset), self.number, self.text)
        
    def sameas(self):
        if self.type == 'sameas':
            try:
                self.__sameas = res = getattr(self, "__sameas", 
                    Question.objects.get(number=self.checks, 
                        questionset__questionnaire=self.questionset.questionnaire))
                return res
            except Question.DoesNotExist:
                return Question(type='comment') # replace with something benign
        return self

    def display_number(self):
        "Return either the number alone or the non-number part of the question number indented"
        m = _numre.match(self.number)
        if m:
            sub = m.group(2)
            return "&nbsp;&nbsp;&nbsp;" + sub
        return self.number

    def choices(self):
        if self.type == 'sameas':
            return self.sameas().choices()
        res = Choice.objects.filter(question=self).order_by('sortid')
        return res

    def is_custom(self):
        return "custom" == self.sameas().type

    def get_type(self):
        "Get the type name, treating sameas and custom specially"
        t = self.sameas().type
        if t == 'custom':
            cd = self.sameas().getcheckdict()
            if 'type' not in cd:
                raise Exception("When using custom types, you must have type=<name> in the additional checks field")
            return cd.get('type')
        return t

    def questioninclude(self):
        return "questionnaire/" + self.get_type() + ".html"

    def __cmp__(a, b):
        anum, astr = split_numal(a.number)
        bnum, bstr = split_numal(b.number)
        cmpnum = cmp(anum, bnum)
        return cmpnum or cmp(astr, bstr)

    class Meta:
        translate = ('text', 'extra', 'footer')

    def dump_json_data(self, schema_version=1):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text', 'extra'
        return dict(
            choices=[ch.dump_json_data(schema_version)
                for ch in self.choice_set.order_by('sortid')],
            **attrs_to_dict(self,
                ('number', 'text', 'type', 'extra', 'checks')))


class ChoiceManager(models.Manager):
    def create_from_json_data(self, data, schema_version=1, question=None):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text'
        return self.create(
            question=question,
            **dict_filter_keys(data, ('sortid', 'value', 'text')))


class Choice(models.Model):
    __metaclass__ = TransMeta
    objects = ChoiceManager()

    question = models.ForeignKey(Question)
    sortid = models.IntegerField()
    value = models.CharField(u"Short Value", max_length=64)
    text = models.CharField(u"Choice Text", max_length=200)

    def __unicode__(self):
        return u'(%s) %d. %s' % (self.question.number, self.sortid, self.text)

    class Meta:
        translate = ('text',)

    def dump_json_data(self, schema_version=1):
        support_only_versions(schema_version, (1,))
        # FIXME: support multiple languages in 'text'
        return attrs_to_dict(self, ('sortid', 'value', 'text'))


class Answer(models.Model):
    subject = models.ForeignKey(Subject, help_text = u'The user who supplied this answer')
    question = models.ForeignKey(Question, help_text = u"The question that this is an answer to")
    runid = models.CharField(u'RunID', help_text = u"The RunID (ie. year)", max_length=32)
    answer = models.TextField()

    def __unicode__(self):
        return "Answer(%s: %s, %s)" % (self.question.number, self.subject.surname, self.subject.givenname)

    def choice_str(self, secondary = False):
        choice_string = ""
        choices = self.question.get_choices()

        for choice in choices:
            for split_answer in self.split_answer():
                if str(split_answer) == choice.value:
                    choice_string += str(choice.text) + " "

    def split_answer(self):
        """
        Decode stored answer value and return as a list of choices.
        Any freeform value will be returned in a list as the last item.

        Calling code should be tolerant of freeform answers outside
        of additional [] if data has been stored in plain text format
        """
        try:
            return json.loads(self.answer)
        except ValueError:
            # this was likely saved as plain text, try to guess what the 
            # value(s) were
            if 'multiple' in self.question.type:
                return self.answer.split('; ')
            else:
                return [self.answer]

    def check_answer(self):
        "Confirm that the supplied answer matches what we expect"
        return True


# Utility functions for json data import/export methods above

def attrs_to_dict(obj, names):
    return dict((n, getattr(obj, n)) for n in names)

def dict_filter_keys(d, keys):
    return dict((n, d[n]) for n in keys)

def support_only_versions(schema_version, versions):
    if schema_version not in versions:
        raise NotImplementedError("schema version not supported: %r" %
            schema_version)
