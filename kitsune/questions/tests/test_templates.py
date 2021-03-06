# -*- coding: utf-8 -*-
import json
import random
from datetime import datetime, timedelta
from string import letters

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core import mail
from django.core.cache import cache

import mock
from nose.tools import eq_
from pyquery import PyQuery as pq
from taggit.models import Tag
from tidings.models import Watch

import kitsune.questions.tasks
from kitsune.products.tests import product
from kitsune.questions.events import QuestionReplyEvent, QuestionSolvedEvent
from kitsune.questions.models import (
    Question, Answer, VoteMetadata, QuestionLocale)
from kitsune.questions.tests import (
    TestCaseBase, tags_eq, question, answer, answervote)
from kitsune.questions.views import UNAPPROVED_TAG, NO_TAG
from kitsune.search.tests import ElasticTestCase
from kitsune.sumo.helpers import urlparams
from kitsune.sumo.tests import (
    get, post, attrs_eq, emailmessage_raise_smtp, TestCase, LocalizingClient)
from kitsune.sumo.urlresolvers import reverse
from kitsune.tags.tests import tag
from kitsune.products.tests import topic
from kitsune.upload.models import ImageAttachment
from kitsune.users.models import RegistrationProfile
from kitsune.users.tests import user, add_permission
from kitsune.wiki.tests import document, revision


class AnswersTemplateTestCase(TestCaseBase):
    """Test the Answers template."""
    def setUp(self):
        super(AnswersTemplateTestCase, self).setUp()

        self.user = user(save=True)
        self.client.login(username=self.user.username, password='testpass')
        self.question = answer(save=True).question
        self.answer = self.question.answers.all()[0]

    def test_answer(self):
        """Posting a valid answer inserts it."""
        num_answers = self.question.answers.count()
        content = 'lorem ipsum dolor sit amet'
        response = post(self.client, 'questions.reply',
                        {'content': content},
                        args=[self.question.id])

        eq_(1, len(response.redirect_chain))
        eq_(num_answers + 1, self.question.answers.count())

        new_answer = self.question.answers.order_by('-id')[0]
        eq_(content, new_answer.content)
        # Check canonical url
        doc = pq(response.content)
        eq_('/questions/{0!s}'.format(self.question.id),
            doc('link[rel="canonical"]')[0].attrib['href'])

    def test_answer_upload(self):
        """Posting answer attaches an existing uploaded image to the answer."""
        f = open('kitsune/upload/tests/media/test.jpg')
        post(self.client, 'upload.up_image_async', {'image': f},
             args=['auth.User', self.user.pk])
        f.close()

        content = 'lorem ipsum dolor sit amet'
        response = post(self.client, 'questions.reply',
                        {'content': content},
                        args=[self.question.id])
        eq_(200, response.status_code)

        new_answer = self.question.answers.order_by('-id')[0]
        eq_(1, new_answer.images.count())
        image = new_answer.images.all()[0]
        name = '098f6b.png'
        message = 'File name "{0!s}" does not contain "{1!s}"'.format(
            image.file.name, name)
        assert name in image.file.name, message
        eq_(self.user.username, image.creator.username)

        # Clean up
        ImageAttachment.objects.all().delete()

    def test_empty_answer(self):
        """Posting an empty answer shows error."""
        response = post(self.client, 'questions.reply', {'content': ''},
                        args=[self.question.id])

        doc = pq(response.content)
        error_msg = doc('ul.errorlist li a')[0]
        eq_(error_msg.text, 'Please provide content.')

    def test_short_answer(self):
        """Posting a short answer shows error."""
        response = post(self.client, 'questions.reply', {'content': 'lor'},
                        args=[self.question.id])

        doc = pq(response.content)
        error_msg = doc('ul.errorlist li a')[0]
        eq_(error_msg.text, 'Your content is too short (3 characters). ' +
                            'It must be at least 5 characters.')

    def test_long_answer(self):
        """Post a long answer shows error."""
        # Set up content length to 10,001 characters
        content = ''
        for i in range(1000):
            content += '1234567890'
        content += '1'

        response = post(self.client, 'questions.reply', {'content': content},
                        args=[self.question.id])

        doc = pq(response.content)
        error_msg = doc('ul.errorlist li a')[0]
        eq_(error_msg.text, 'Please keep the length of your content to ' +
                            '10,000 characters or less. It is currently ' +
                            '10,001 characters.')

    def test_solve_unsolve(self):
        """Test accepting a solution and undoing."""
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('div.solution')))

        ans = self.question.answers.all()[0]
        # Sign in as asker, solve and verify
        self.client.login(
            username=self.question.creator.username, password='testpass')
        response = post(self.client, 'questions.solve',
                        args=[self.question.id, ans.id])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('div.solution')))
        div = doc('h3.is-solution')[0].getparent().getparent()
        eq_('answer-{0!s}'.format(ans.id), div.attrib['id'])
        q = Question.objects.get(pk=self.question.id)
        eq_(q.solution, ans)
        eq_(q.solver, self.question.creator)

        # Try to solve again with different answer. It shouldn't blow up or
        # change the solution.
        answer(question=q, save=True)
        response = post(self.client, 'questions.solve',
                        args=[self.question.id, ans.id])
        eq_(200, response.status_code)
        q = Question.objects.get(pk=self.question.id)
        eq_(q.solution, ans)

        # Unsolve and verify
        response = post(self.client, 'questions.unsolve',
                        args=[self.question.id, ans.id])
        q = Question.objects.get(pk=self.question.id)
        eq_(q.solution, None)
        eq_(q.solver, None)

    def test_only_owner_or_admin_can_solve_unsolve(self):
        """Make sure non-owner/non-admin can't solve/unsolve."""
        # Try as asker
        self.client.login(
            username=self.question.creator.username, password='testpass')
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('input[name="solution"]')))
        self.client.logout()

        # Try as a nobody
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('input[name="solution"]')))

        ans = self.question.answers.all()[0]
        # Try to solve
        response = post(self.client, 'questions.solve',
                        args=[self.question.id, ans.id])
        eq_(403, response.status_code)
        # Try to unsolve
        response = post(self.client, 'questions.unsolve',
                        args=[self.question.id, ans.id])
        eq_(403, response.status_code)

    def test_solve_unsolve_with_perm(self):
        """Test marking solve/unsolve with 'change_solution' permission."""
        u = user(save=True)
        add_permission(u, Question, 'change_solution')
        self.client.login(username=u.username, password='testpass')
        ans = self.question.answers.all()[0]
        # Solve and verify
        post(self.client, 'questions.solve',
             args=[self.question.id, ans.id])
        q = Question.objects.get(pk=self.question.id)
        eq_(q.solution, ans)
        eq_(q.solver, u)
        # Unsolve and verify
        post(self.client, 'questions.unsolve',
             args=[self.question.id, ans.id])
        q = Question.objects.get(pk=self.question.id)
        eq_(q.solution, None)
        eq_(q.solver, None)

    def test_needs_info_checkbox(self):
        """Test that needs info checkbox is correctly shown"""
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('input[name="needsinfo"]')))

        self.question.set_needs_info()

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('input[name="clear_needsinfo"]')))

    def test_question_vote_GET(self):
        """Attempting to vote with HTTP GET returns a 405."""
        response = get(self.client, 'questions.vote',
                       args=[self.question.id])
        eq_(405, response.status_code)

    def common_vote(self, me_too_count=1):
        """Helper method for question vote tests."""
        # Check that there are no votes and vote form renders
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        assert '0\n' in doc('.have-problem')[0].text
        eq_(me_too_count, len(doc('div.me-too form')))

        # Vote
        ua = 'Mozilla/5.0 (DjangoTestClient)'
        self.client.post(reverse('questions.vote', args=[self.question.id]),
                         {}, HTTP_USER_AGENT=ua)

        # Check that there is 1 vote and vote form doesn't render
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        assert '1\n' in doc('.have-problem')[0].text
        eq_(0, len(doc('div.me-too form')))
        # Verify user agent
        vote_meta = VoteMetadata.objects.all()[0]
        eq_('ua', vote_meta.key)
        eq_(ua, vote_meta.value)

        # Voting again (same user) should not increment vote count
        post(self.client, 'questions.vote', args=[self.question.id])
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        assert '1\n' in doc('.have-problem')[0].text

    def test_question_authenticated_vote(self):
        """Authenticated user vote."""
        # Common vote test
        self.common_vote()

    def test_question_anonymous_vote(self):
        """Anonymous user vote."""
        # Log out
        self.client.logout()

        # Common vote test
        self.common_vote(2)

    def common_answer_vote(self):
        """Helper method for answer vote tests."""
        # Check that there are no votes and vote form renders
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('form.helpful input[name="helpful"]')))

        # Vote
        ua = 'Mozilla/5.0 (DjangoTestClient)'
        self.client.post(reverse('questions.answer_vote',
                                 args=[self.question.id, self.answer.id]),
                         {'helpful': 'y'}, HTTP_USER_AGENT=ua)

        # Check that there is 1 vote and vote form doesn't render
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)

        eq_(1, len(doc('#answer-{0!s} h3.is-helpful'.format(self.answer.id))))
        eq_(0, len(doc('form.helpful input[name="helpful"]')))
        # Verify user agent
        vote_meta = VoteMetadata.objects.all()[0]
        eq_('ua', vote_meta.key)
        eq_(ua, vote_meta.value)

    def test_answer_authenticated_vote(self):
        """Authenticated user answer vote."""
        # log in as new user (didn't ask or answer question)
        self.client.logout()
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')

        # Common vote test
        self.common_answer_vote()

    def test_answer_anonymous_vote(self):
        """Anonymous user answer vote."""
        # Log out
        self.client.logout()

        # Common vote test
        self.common_answer_vote()

    def test_can_vote_on_asker_reply(self):
        """An answer posted by the asker can be voted on."""
        self.client.logout()
        # Post a new answer by the asker => two votable answers
        q = self.question
        Answer.objects.create(question=q, creator=q.creator, content='test')
        response = get(self.client, 'questions.details', args=[q.id])
        doc = pq(response.content)
        eq_(2, len(doc('form.helpful input[name="helpful"]')))

    def test_asker_can_vote(self):
        """The asker can vote Not/Helpful."""
        self.client.login(username=self.question.creator.username,
                          password='testpass')
        self.common_answer_vote()

    def test_can_solve_with_answer_by_asker(self):
        """An answer posted by the asker can be the solution."""
        self.client.login(username=self.question.creator.username,
                          password='testpass')
        # Post a new answer by the asker => two solvable answers
        q = self.question
        Answer.objects.create(question=q, creator=q.creator, content='test')
        response = get(self.client, 'questions.details', args=[q.id])
        doc = pq(response.content)
        eq_(2, len(doc('form.solution input[name="solution"]')))

    def test_delete_question_without_permissions(self):
        """Deleting a question without permissions is a 403."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.delete',
                       args=[self.question.id])
        eq_(403, response.status_code)
        response = post(self.client, 'questions.delete',
                        args=[self.question.id])
        eq_(403, response.status_code)

    def test_delete_question_logged_out(self):
        """Deleting a question while logged out redirects to login."""
        self.client.logout()
        response = get(self.client, 'questions.delete',
                       args=[self.question.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/{0!s}{1!s}?next=/en-US/questions/{2!s}/delete'.format(settings.LANGUAGE_CODE, settings.LOGIN_URL, self.question.id),
            redirect[0])

        response = post(self.client, 'questions.delete',
                        args=[self.question.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/{0!s}{1!s}?next=/en-US/questions/{2!s}/delete'.format(settings.LANGUAGE_CODE, settings.LOGIN_URL, self.question.id),
            redirect[0])

    def test_delete_question_with_permissions(self):
        """Deleting a question with permissions."""
        u = user(save=True)
        add_permission(u, Question, 'delete_question')
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.delete',
                       args=[self.question.id])
        eq_(200, response.status_code)

        response = post(self.client, 'questions.delete',
                        args=[self.question.id])
        eq_(0, Question.objects.filter(pk=self.question.id).count())

    def test_delete_answer_without_permissions(self):
        """Deleting an answer without permissions sends 403."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        ans = self.question.last_answer
        response = get(self.client, 'questions.delete_answer',
                       args=[self.question.id, ans.id])
        eq_(403, response.status_code)

        response = post(self.client, 'questions.delete_answer',
                        args=[self.question.id, ans.id])
        eq_(403, response.status_code)

    def test_delete_answer_logged_out(self):
        """Deleting an answer while logged out redirects to login."""
        self.client.logout()
        q = self.question
        ans = q.last_answer
        response = get(self.client, 'questions.delete_answer',
                       args=[self.question.id, ans.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/{0!s}{1!s}?next=/en-US/questions/{2!s}/delete/{3!s}'.format(settings.LANGUAGE_CODE, settings.LOGIN_URL, q.id, ans.id),
            redirect[0])

        response = post(self.client, 'questions.delete_answer',
                        args=[self.question.id, ans.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/{0!s}{1!s}?next=/en-US/questions/{2!s}/delete/{3!s}'.format(settings.LANGUAGE_CODE, settings.LOGIN_URL, q.id, ans.id),
            redirect[0])

    def test_delete_answer_with_permissions(self):
        """Deleting an answer with permissions."""
        ans = self.question.last_answer
        u = user(save=True)
        add_permission(u, Answer, 'delete_answer')
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.delete_answer',
                       args=[self.question.id, ans.id])
        eq_(200, response.status_code)

        response = post(self.client, 'questions.delete_answer',
                        args=[self.question.id, ans.id])
        eq_(0, Answer.objects.filter(pk=self.question.id).count())

    def test_edit_answer_without_permission(self):
        """Editing an answer without permissions returns a 403.

        The edit link shouldn't show up on the Answers page."""
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('ol.answers li.edit')))

        answer = self.question.last_answer
        response = get(self.client, 'questions.edit_answer',
                       args=[self.question.id, answer.id])
        eq_(403, response.status_code)

        content = 'New content for answer'
        response = post(self.client, 'questions.edit_answer',
                        {'content': content},
                        args=[self.question.id, answer.id])
        eq_(403, response.status_code)

    def test_edit_answer_with_permissions(self):
        """Editing an answer with permissions.

        The edit link should show up on the Answers page."""
        u = user(save=True)
        add_permission(u, Answer, 'change_answer')
        self.client.login(username=u.username, password='testpass')

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('li.edit')))

        answer = self.question.last_answer
        response = get(self.client, 'questions.edit_answer',
                       args=[self.question.id, answer.id])
        eq_(200, response.status_code)

        content = 'New content for answer'
        response = post(self.client, 'questions.edit_answer',
                        {'content': content},
                        args=[self.question.id, answer.id])
        eq_(content, Answer.objects.get(pk=answer.id).content)

    def test_answer_creator_can_edit(self):
        """The creator of an answer can edit his/her answer."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')

        # Initially there should be no edit links
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('ol.answers li.edit')))

        # Add an answer and verify the edit link shows up
        content = 'lorem ipsum dolor sit amet'
        response = post(self.client, 'questions.reply',
                        {'content': content},
                        args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('li.edit')))
        new_answer = self.question.answers.order_by('-id')[0]
        eq_(1, len(doc('#answer-{0!s} + div li.edit'.format(new_answer.id))))

        # Make sure it can be edited
        content = 'New content for answer'
        response = post(self.client, 'questions.edit_answer',
                        {'content': content},
                        args=[self.question.id, new_answer.id])
        eq_(200, response.status_code)

        # Now lock it and make sure it can't be edited
        self.question.is_locked = True
        self.question.save()
        response = post(self.client, 'questions.edit_answer',
                        {'content': content},
                        args=[self.question.id, new_answer.id])
        eq_(403, response.status_code)

    def test_lock_question_without_permissions(self):
        """Trying to lock a question without permission is a 403."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        q = self.question
        response = post(self.client, 'questions.lock', args=[q.id])
        eq_(403, response.status_code)

    def test_lock_question_logged_out(self):
        """Trying to lock a question while logged out redirects to login."""
        self.client.logout()
        q = self.question
        response = post(self.client, 'questions.lock', args=[q.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/{0!s}{1!s}?next=/en-US/questions/{2!s}/lock'.format(settings.LANGUAGE_CODE, settings.LOGIN_URL, q.id), redirect[0])

    def test_lock_question_with_permissions_GET(self):
        """Trying to lock a question via HTTP GET."""
        u = user(save=True)
        add_permission(u, Question, 'lock_question')
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.lock', args=[self.question.id])
        eq_(405, response.status_code)

    def test_lock_question_with_permissions_POST(self):
        """Locking questions with permissions via HTTP POST."""
        u = user(save=True)
        add_permission(u, Question, 'lock_question')
        self.client.login(username=u.username, password='testpass')
        q = self.question
        response = post(self.client, 'questions.lock', args=[q.id])
        eq_(200, response.status_code)
        eq_(True, Question.objects.get(pk=q.pk).is_locked)
        assert 'This thread was closed.' in response.content

        # now unlock it
        response = post(self.client, 'questions.lock', args=[q.id])
        eq_(200, response.status_code)
        eq_(False, Question.objects.get(pk=q.pk).is_locked)

    def test_reply_to_locked_question(self):
        """Locked questions can't be answered."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')

        # Without add_answer permission, we should 403.
        q = self.question
        q.is_locked = True
        q.save()
        response = post(self.client, 'questions.reply',
                        {'content': 'just testing'}, args=[q.id])
        eq_(403, response.status_code)

        # With add_answer permission, it should work.
        add_permission(u, Answer, 'add_answer')
        response = post(self.client, 'questions.reply',
                        {'content': 'just testing'}, args=[q.id])
        eq_(200, response.status_code)

    def test_edit_answer_locked_question(self):
        """Verify edit answer of a locked question only with permissions."""
        self.question.is_locked = True
        self.question.save()

        # The answer creator can't edit if question is locked
        u = self.question.last_answer.creator
        self.client.login(username=u.username, password='testpass')

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('li.edit')))

        answer = self.question.last_answer
        response = get(self.client, 'questions.edit_answer',
                       args=[self.question.id, answer.id])
        eq_(403, response.status_code)

        # A user with edit_answer permission can edit.
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        add_permission(u, Answer, 'change_answer')

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('li.edit')))

        answer = self.question.last_answer
        response = get(self.client, 'questions.edit_answer',
                       args=[self.question.id, answer.id])
        eq_(200, response.status_code)

        content = 'New content for answer'
        response = post(self.client, 'questions.edit_answer',
                        {'content': content},
                        args=[self.question.id, answer.id])
        eq_(content, Answer.objects.get(pk=answer.id).content)

    def test_vote_locked_question_403(self):
        """Locked questions can't be voted on."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')

        q = self.question
        q.is_locked = True
        q.save()
        response = post(self.client, 'questions.vote', args=[q.id])
        eq_(403, response.status_code)

    def test_vote_answer_to_locked_question_403(self):
        """Answers to locked questions can't be voted on."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')

        q = self.question
        q.is_locked = True
        q.save()
        response = post(self.client, 'questions.answer_vote',
                        {'helpful': 'y'}, args=[q.id, self.answer.id])
        eq_(403, response.status_code)

    def test_watch_GET_405(self):
        """Watch replies with HTTP GET results in 405."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.watch',
                       args=[self.question.id])
        eq_(405, response.status_code)

    def test_unwatch_GET_405(self):
        """Unwatch replies with HTTP GET results in 405."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        response = get(self.client, 'questions.unwatch',
                       args=[self.question.id])
        eq_(405, response.status_code)

    @mock.patch.object(Site.objects, 'get_current')
    def test_watch_replies(self, get_current):
        """Watch a question for replies."""
        get_current.return_value.domain = 'testserver'
        self.client.logout()

        # Delete existing watches
        Watch.objects.all().delete()

        post(self.client, 'questions.watch',
             {'email': 'some@bo.dy', 'event_type': 'reply'},
             args=[self.question.id])
        assert QuestionReplyEvent.is_notifying('some@bo.dy', self.question), (
            'Watch was not created')

        attrs_eq(mail.outbox[1], to=['some@bo.dy'],
                 subject='Please confirm your email address')
        assert 'questions/confirm/' in mail.outbox[1].body
        assert 'New answers' in mail.outbox[1].body

        # Now activate the watch.
        w = Watch.objects.get()
        get(self.client, 'questions.activate_watch', args=[w.id, w.secret])
        assert Watch.objects.get(id=w.id).is_active

    @mock.patch.object(mail.EmailMessage, 'send')
    def test_watch_replies_smtp_error(self, emailmessage_send):
        """Watch a question for replies and fail to send email."""
        emailmessage_send.side_effect = emailmessage_raise_smtp
        self.client.logout()

        r = post(self.client, 'questions.watch',
                 {'email': 'some@bo.dy', 'event_type': 'reply'},
                 args=[self.question.id])
        assert not QuestionReplyEvent.is_notifying(
            'some@bo.dy', self.question), 'Watch was created'
        self.assertContains(r, 'Could not send a message to that email')

    @mock.patch.object(Site.objects, 'get_current')
    def test_watch_replies_wrong_secret(self, get_current):
        """Watch a question for replies."""
        # This also covers test_watch_solution_wrong_secret.
        get_current.return_value.domain = 'testserver'
        self.client.logout()

        # Delete existing watches
        Watch.objects.all().delete()

        post(self.client, 'questions.watch',
             {'email': 'some@bo.dy', 'event_type': 'reply'},
             args=[self.question.id])

        # Now activate the watch.
        w = Watch.objects.get()
        r = get(self.client, 'questions.activate_watch', args=[w.id, 'fail'])
        eq_(200, r.status_code)
        assert not Watch.objects.get(id=w.id).is_active

    def test_watch_replies_logged_in(self):
        """Watch a question for replies (logged in)."""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        u = User.objects.get(username=u.username)
        post(self.client, 'questions.watch',
             {'event_type': 'reply'},
             args=[self.question.id])
        assert QuestionReplyEvent.is_notifying(u, self.question), (
            'Watch was not created')
        return u

    @mock.patch.object(Site.objects, 'get_current')
    def test_watch_solution(self, get_current):
        """Watch a question for solution."""
        self.client.logout()
        get_current.return_value.domain = 'testserver'

        # Delete existing watches
        Watch.objects.all().delete()

        post(self.client, 'questions.watch',
             {'email': 'some@bo.dy', 'event_type': 'solution'},
             args=[self.question.id])
        assert QuestionSolvedEvent.is_notifying('some@bo.dy', self.question), (
            'Watch was not created')

        attrs_eq(mail.outbox[1], to=['some@bo.dy'],
                 subject='Please confirm your email address')
        assert 'questions/confirm/' in mail.outbox[1].body
        assert 'Solution found' in mail.outbox[1].body

        # Now activate the watch.
        w = Watch.objects.get()
        get(self.client, 'questions.activate_watch', args=[w.id, w.secret])
        assert Watch.objects.get().is_active

    def test_unwatch(self):
        """Unwatch a question."""
        # First watch question.
        u = self.test_watch_replies_logged_in()
        # Then unwatch it.
        self.client.login(username=u.username, password='testpass')
        post(self.client, 'questions.unwatch', args=[self.question.id])
        assert not QuestionReplyEvent.is_notifying(u, self.question), (
            'Watch was not destroyed')

    def test_watch_solution_and_replies(self):
        """User subscribes to solution and replies: page doesn't break"""
        u = user(save=True)
        self.client.login(username=u.username, password='testpass')
        QuestionReplyEvent.notify(u, self.question)
        QuestionSolvedEvent.notify(u, self.question)
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        eq_(200, response.status_code)

    def test_preview_answer(self):
        """Preview an answer."""
        num_answers = self.question.answers.count()
        content = 'Awesome answer.'
        response = post(self.client, 'questions.reply',
                        {'content': content, 'preview': 'any string'},
                        args=[self.question.id])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(content, doc('#answer-preview div.content').text())
        eq_(num_answers, self.question.answers.count())

    def test_preview_answer_as_admin(self):
        """Preview an answer as admin and verify response is 200."""
        u = user(is_staff=True, is_superuser=True, save=True)
        self.client.login(username=u.username, password='testpass')
        content = 'Awesome answer.'
        response = post(self.client, 'questions.reply',
                        {'content': content, 'preview': 'any string'},
                        args=[self.question.id])
        eq_(200, response.status_code)

    def test_links_nofollow(self):
        """Links posted in questions and answers should have rel=nofollow."""
        q = self.question
        q.content = 'lorem http://ipsum.com'
        q.save()
        a = self.answer
        a.content = 'testing http://example.com'
        a.save()
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_('nofollow', doc('.question .main-content a')[0].attrib['rel'])
        eq_('nofollow', doc('.answer .main-content a')[0].attrib['rel'])

    def test_robots_noindex_unsolved(self):
        """Verify noindex on unsolved questions."""
        q = question(save=True)

        # A brand new questions should be noindexed...
        response = get(self.client, 'questions.details', args=[q.id])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('meta[name=robots]')))

        # If it has one answer, it should still be noindexed...
        a = answer(question=q, save=True)
        response = get(self.client, 'questions.details', args=[q.id])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('meta[name=robots]')))

        # If the answer is the solution, then it shouldn't be noindexed
        # anymore.
        q.solution = a
        q.save()
        response = get(self.client, 'questions.details', args=[q.id])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(0, len(doc('meta[name=robots]')))


class TaggingViewTestsAsTagger(TestCaseBase):
    """Tests for views that add and remove tags, logged in as someone who can
    add and remove but not create tags

    Also hits the tag-related parts of the answer template.

    """
    def setUp(self):
        super(TaggingViewTestsAsTagger, self).setUp()

        u = user(save=True)
        add_permission(u, Question, 'tag_question')
        self.client.login(username=u.username, password='testpass')

        self.question = question(content=u'lorém ipsuñ', save=True)

    # add_tag view:

    def test_add_tag_get_method(self):
        """Assert GETting the add_tag view redirects to the answers page."""
        response = self.client.get(_add_tag_url(self.question.id))
        url = 'http://testserver{0!s}'.format(reverse(
            'questions.details',
            kwargs={'question_id': self.question.id},
            force_locale=True))
        self.assertRedirects(response, url)

    def test_add_nonexistent_tag(self):
        """Assert adding a nonexistent tag sychronously shows an error."""
        response = self.client.post(_add_tag_url(self.question.id),
                                    data={'tag-name': 'nonexistent tag'})
        self.assertContains(response, UNAPPROVED_TAG)

    def test_add_existent_tag(self):
        """Test adding a tag, case insensitivity, and space stripping."""
        tag(name='PURplepurplepurple', slug='purplepurplepurple', save=True)
        response = self.client.post(_add_tag_url(self.question.id),
                                    data={'tag-name': ' PURplepurplepurple '},
                                    follow=True)
        self.assertContains(response, 'purplepurplepurple')

    def test_add_no_tag(self):
        """Make sure adding a blank tag shows an error message."""
        response = self.client.post(_add_tag_url(self.question.id),
                                    data={'tag-name': ''})
        self.assertContains(response, NO_TAG)

    # add_tag_async view:

    def test_add_async_nonexistent_tag(self):
        """Assert adding an nonexistent tag yields an AJAX error."""
        response = self.client.post(_add_async_tag_url(self.question.id),
                                    data={'tag-name': 'nonexistent tag'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertContains(response, UNAPPROVED_TAG, status_code=400)

    def test_add_async_existent_tag(self):
        """Assert adding an unapplied tag."""
        tag(name='purplepurplepurple', slug='purplepurplepurple', save=True)
        response = self.client.post(_add_async_tag_url(self.question.id),
                                    data={'tag-name': ' PURplepurplepurple '},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertContains(response, 'canonicalName')
        tags = Question.objects.get(id=self.question.id).tags.all()

        # Test the backend since we don't have a newly rendered page to
        # rely on.
        eq_([t.name for t in tags], ['purplepurplepurple'])

    def test_add_async_no_tag(self):
        """Assert adding an empty tag asynchronously yields an AJAX error."""
        response = self.client.post(_add_async_tag_url(self.question.id),
                                    data={'tag-name': ''},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertContains(response, NO_TAG, status_code=400)

    # remove_tag view:

    def test_remove_applied_tag(self):
        """Assert removing an applied tag succeeds."""
        self.question.tags.add('green')
        self.question.tags.add('colorless')
        response = self.client.post(_remove_tag_url(self.question.id),
                                    data={'remove-tag-colorless': 'dummy'})
        self._assert_redirects_to_question(response, self.question.id)
        tags = Question.objects.get(pk=self.question.id).tags.all()
        eq_([t.name for t in tags], ['green'])

    def test_remove_unapplied_tag(self):
        """Test removing an unapplied tag fails silently."""
        response = self.client.post(_remove_tag_url(self.question.id),
                                    data={'remove-tag-lemon': 'dummy'})
        self._assert_redirects_to_question(response, self.question.id)

    def test_remove_no_tag(self):
        """Make sure removing with no params provided redirects harmlessly."""
        response = self.client.post(_remove_tag_url(self.question.id),
                                    data={})
        self._assert_redirects_to_question(response, self.question.id)

    def _assert_redirects_to_question(self, response, question_id):
        url = 'http://testserver{0!s}'.format(reverse(
            'questions.details', kwargs={'question_id': question_id},
            force_locale=True))
        self.assertRedirects(response, url)

    # remove_tag_async view:

    def test_remove_async_applied_tag(self):
        """Assert taking a tag off a question works."""
        self.question.tags.add('green')
        self.question.tags.add('colorless')
        response = self.client.post(_remove_async_tag_url(self.question.id),
                                    data={'name': 'colorless'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        eq_(response.status_code, 200)
        tags = Question.objects.get(pk=self.question.id).tags.all()
        eq_([t.name for t in tags], ['green'])

    def test_remove_async_unapplied_tag(self):
        """Assert trying to remove a tag that isn't there succeeds."""
        response = self.client.post(_remove_async_tag_url(self.question.id),
                                    data={'name': 'lemon'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        eq_(response.status_code, 200)

    def test_remove_async_no_tag(self):
        """Assert calling the remove handler with no param fails."""
        response = self.client.post(_remove_async_tag_url(self.question.id),
                                    data={},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertContains(response, NO_TAG, status_code=400)

    @mock.patch.object(kitsune.questions.tasks, 'submit_ticket')
    @mock.patch.object(Site.objects, 'get_current')
    def test_escalate_tag(self, get_current, submit_ticket):
        """Verify that tagging a question "escalate" submits to zendesk."""
        get_current.return_value.domain = 'testserver'

        tag(name='escalate', slug='escalate', save=True)
        self.client.post(
            _add_tag_url(self.question.id),
            data={'tag-name': 'escalate'},
            follow=True)

        question_url = u'https://testserver/en-US{url}'.format(
            url=self.question.get_absolute_url())
        submit_ticket.assert_called_with(
            email='support@mozilla.com',
            category='Escalated',
            subject=u'[Escalated] {title}'.format(title=self.question.title),
            body=u'{url}\n\n{content}'.format(
                url=question_url, content=self.question.content),
            tags=['escalate'])


class TaggingViewTestsAsAdmin(TestCaseBase):
    """Tests for views that create new tags, logged in as someone who can"""

    def setUp(self):
        super(TaggingViewTestsAsAdmin, self).setUp()

        u = user(save=True)
        add_permission(u, Question, 'tag_question')
        add_permission(u, Tag, 'add_tag')
        self.client.login(username=u.username, password='testpass')

        self.question = question(save=True)
        tag(name='red', slug='red', save=True)

    def test_add_new_tag(self):
        """Assert adding a nonexistent tag sychronously creates & adds it."""
        self.client.post(
            _add_tag_url(self.question.id),
            data={'tag-name': 'nonexistent tag'})
        tags_eq(Question.objects.get(id=self.question.id),
                ['nonexistent tag'])

    def test_add_async_new_tag(self):
        """Assert adding an nonexistent tag creates & adds it."""
        response = self.client.post(_add_async_tag_url(self.question.id),
                                    data={'tag-name': 'nonexistent tag'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        eq_(response.status_code, 200)
        tags_eq(Question.objects.get(id=self.question.id), ['nonexistent tag'])

    def test_add_new_case_insensitive(self):
        """Adding a tag differing only in case from existing ones shouldn't
        create a new tag."""
        self.client.post(_add_async_tag_url(self.question.id),
                         data={'tag-name': 'RED'},
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        tags_eq(Question.objects.get(id=self.question.id), ['red'])

    def test_add_new_canonicalizes(self):
        """Adding a new tag as an admin should still canonicalize case."""
        response = self.client.post(_add_async_tag_url(self.question.id),
                                    data={'tag-name': 'RED'},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        eq_(json.loads(response.content)['canonicalName'], 'red')


def _add_tag_url(question_id):
    """Return the URL to add_tag for question 1, an untagged question."""
    return reverse('questions.add_tag', kwargs={'question_id': question_id})


def _add_async_tag_url(question_id):
    """Return the URL to add_tag_async for question 1, an untagged question."""
    return reverse(
        'questions.add_tag_async', kwargs={'question_id': question_id})


def _remove_tag_url(question_id):
    """Return  URL to remove_tag for question 2, tagged {colorless, green}."""
    return reverse(
        'questions.remove_tag', kwargs={'question_id': question_id})


def _remove_async_tag_url(question_id):
    """Return URL to remove_tag_async on q. 2, tagged {colorless, green}."""
    return reverse(
        'questions.remove_tag_async', kwargs={'question_id': question_id})


class QuestionsTemplateTestCase(TestCaseBase):
    def test_tagged(self):
        u = user(save=True)
        add_permission(u, Question, 'tag_question')
        tagname = 'mobile'
        tag(name=tagname, slug=tagname, save=True)
        self.client.login(username=u.username, password="testpass")
        tagged = urlparams(reverse(
            'questions.list', args=['all']), tagged=tagname, show='all')

        # First there should be no questions tagged 'mobile'
        response = self.client.get(tagged)
        doc = pq(response.content)
        eq_(0, len(doc('article.questions > section')))

        # Tag a question 'mobile'
        q = question(save=True)
        response = post(self.client, 'questions.add_tag',
                        {'tag-name': tagname},
                        args=[q.id])
        eq_(200, response.status_code)

        # Add an answer
        answer(question=q, save=True)

        # Now there should be 1 question tagged 'mobile'
        response = self.client.get(tagged)
        doc = pq(response.content)
        eq_(1, len(doc('article.questions > section')))
        eq_('/questions/all?tagged=mobile&show=all',
            doc('link[rel="canonical"]')[0].attrib['href'])

        # Test a tag that doesn't exist. It shouldnt blow up.
        url = urlparams(
            reverse('questions.list', args=['all']),
            tagged='garbage-plate',
            show='all')
        response = self.client.get(url)
        eq_(200, response.status_code)

    def test_product_filter(self):
        p1 = product(save=True)
        p2 = product(save=True)
        p3 = product(save=True)

        q1 = question(save=True)
        q2 = question(product=p1, save=True)
        q2.save()
        q3 = question(product=p2, save=True)
        q3.save()

        def check(product, expected):
            url = reverse('questions.list', args=[product])
            response = self.client.get(url)
            doc = pq(response.content)
            # Make sure all questions are there.

            # This won't work, because the test case base adds more tests than
            # we expect in it's setUp(). TODO: Fix that.
            eq_(len(expected), len(doc('.questions > section')))

            for q in expected:
                eq_(1, len(doc('.questions > section[id=question-{0!s}]'.format(q.id))))

        # No filtering -> All questions.
        check('all', [q1, q2, q3])
        # Filter on p1 -> only q2
        check(p1.slug, [q2])
        # Filter on p2 -> only q3
        check(p2.slug, [q3])
        # Filter on p3 -> No results
        check(p3.slug, [])
        # Filter on p1,p2
        check('{0!s},{1!s}'.format(p1.slug, p2.slug), [q2, q3])
        # Filter on p1,p3
        check('{0!s},{1!s}'.format(p1.slug, p3.slug), [q2])
        # Filter on p2,p3
        check('{0!s},{1!s}'.format(p2.slug, p3.slug), [q3])

    def test_topic_filter(self):
        p = product(save=True)
        t1 = topic(product=p, save=True)
        t2 = topic(product=p, save=True)
        t3 = topic(product=p, save=True)

        q1 = question(save=True)
        q2 = question(topic=t1, save=True)
        q3 = question(topic=t2, save=True)

        url = reverse('questions.list', args=['all'])

        def check(filter, expected):
            response = self.client.get(urlparams(url, **filter))
            doc = pq(response.content)
            # Make sure all questions are there.

            # This won't work, because the test case base adds more tests than
            # we expect in it's setUp(). TODO: Fix that.
            # eq_(len(expected), len(doc('.questions > section')))

            for q in expected:
                eq_(1, len(doc('.questions > section[id=question-{0!s}]'.format(q.id))))

        # No filtering -> All questions.
        check({}, [q1, q2, q3])
        # Filter on p1 -> only q2
        check({'topic': t1.slug}, [q2])
        # Filter on p2 -> only q3
        check({'topic': t2.slug}, [q3])
        # Filter on p3 -> No results
        check({'topic': t3.slug}, [])

    def test_robots_noindex(self):
        """Verify the page is set for noindex by robots."""
        response = self.client.get(reverse('questions.list', args=['all']))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('meta[name=robots]')))

    def test_select_in_question(self):
        """Verify we properly escape <select/>."""
        question(
            title='test question lorem ipsum <select></select>',
            content='test question content lorem ipsum <select></select>',
            save=True)
        response = self.client.get(reverse('questions.list', args=['all']))
        assert 'test question lorem ipsum' in response.content
        assert 'test question content lorem ipsum' in response.content
        doc = pq(response.content)
        eq_(0, len(doc('article.questions select')))

    def test_truncated_text_is_stripped(self):
        """Verify we strip html from truncated text."""
        long_str = ''.join(random.choice(letters) for x in xrange(170))
        question(
            content='<p>{0!s}</p>'.format(long_str),
            save=True)
        response = self.client.get(reverse('questions.list', args=['all']))

        # Verify that the <p> was stripped
        assert '<p class="short-text"><p>' not in response.content
        assert '<p class="short-text">{0!s}'.format(long_str[:5]) in response.content

    def test_views(self):
        """Verify the view count is displayed correctly."""
        q = question(save=True)
        q.questionvisits_set.create(visits=1007)
        response = self.client.get(reverse('questions.list', args=['all']))
        doc = pq(response.content)
        eq_('1007 views', doc('div.views').text())

    def test_no_unarchive_on_old_questions(self):
        ques = question(save=True,
                        created=(datetime.now() - timedelta(days=200)),
                        is_archived=True)
        response = get(self.client, 'questions.details', args=[ques.id])
        assert 'Archive this post' not in response.content

    def test_show_is_empty_string_doesnt_500(self):
        question(save=True)
        response = self.client.get(urlparams(reverse('questions.list', args=['all']), show=''))
        eq_(200, response.status_code)

    def test_product_shows_without_tags(self):
        p = product(save=True)
        t = topic(product=p, save=True)
        q = question(topic=t, save=True)

        response = self.client.get(urlparams(reverse('questions.list', args=['all']), show=''))
        doc = pq(response.content)
        tag = doc('#question-{id} .tag-list li img'.format(id=q.id))
        # Even though there are no tags, the product should be displayed.
        assert 'logo-{0}'.format(p.slug) in tag.attr('class')


class QuestionsTemplateTestCaseNoFixtures(TestCase):
    client_class = LocalizingClient

    def test_locked_questions_dont_appear(self):
        """Locked questions are not listed on the no-replies list."""
        question(save=True)
        question(save=True)
        question(is_locked=True, save=True)

        url = reverse('questions.list', args=['all'])
        url = urlparams(url, filter='no-replies')
        response = self.client.get(url)
        doc = pq(response.content)
        eq_(2, len(doc('article.questions > section')))


class QuestionEditingTests(TestCaseBase):
    """Tests for the question-editing view and templates"""

    def setUp(self):
        super(QuestionEditingTests, self).setUp()

        self.user = user(save=True)
        add_permission(self.user, Question, 'change_question')
        self.client.login(username=self.user.username, password='testpass')

    def test_extra_fields(self):
        """The edit-question form should show appropriate metadata fields."""
        question_id = question(save=True).id
        response = get(self.client, 'questions.edit_question',
                       kwargs={'question_id': question_id})
        eq_(response.status_code, 200)

        # Make sure each extra metadata field is in the form:
        doc = pq(response.content)
        q = Question.objects.get(pk=question_id)
        extra_fields = (q.product_config.get('extra_fields', []) +
                        q.category_config.get('extra_fields', []))
        for field in extra_fields:
            assert (doc('input[name={0!s}]'.format(field)) or
                    doc('textarea[name={0!s}]'.format(field))), (
                "The {0!s} field is missing from the edit page.".format(field))

    def test_no_extra_fields(self):
        """The edit-question form shouldn't show inappropriate metadata."""
        question_id = question(save=True).id
        response = get(self.client, 'questions.edit_question',
                       kwargs={'question_id': question_id})
        eq_(response.status_code, 200)

        # Take the "os" field as representative. Make sure it doesn't show up:
        doc = pq(response.content)
        assert not doc('input[name=os]')

    def test_post(self):
        """Posting a valid edit form should save the question."""
        p = product(slug='desktop', save=True)
        q = question(product=p, save=True)
        response = post(self.client, 'questions.edit_question',
                        {'title': 'New title',
                         'content': 'New content',
                         'ff_version': 'New version'},
                        kwargs={'question_id': q.id})

        # Make sure the form redirects and thus appears to succeed:
        url = 'http://testserver{0!s}'.format(reverse('questions.details',
                                              kwargs={'question_id': q.id},
                                              force_locale=True))
        self.assertRedirects(response, url)

        # Make sure the static fields, the metadata, and the updated_by field
        # changed:
        q = Question.objects.get(pk=q.id)
        eq_(q.title, 'New title')
        eq_(q.content, 'New content')
        eq_(q.updated_by, self.user)


class AAQTemplateTestCase(TestCaseBase):
    """Test the AAQ template."""
    data = {'title': 'A test question',
            'content': 'I have this question that I hope...',
            'sites_affected': 'http://example.com',
            'ff_version': '3.6.6',
            'os': 'Intel Mac OS X 10.6',
            'plugins': '* Shockwave Flash 10.1 r53',
            'useragent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.8.3) '
                         'Gecko/20120221 Firefox/18.0',
            'troubleshooting': '''{
                "accessibility": {
                    "isActive": true
                },
                "application": {
                    "name": "Firefox",
                    "supportURL": "Some random url.",
                    "userAgent": "A user agent.",
                    "version": "18.0.2"
                },
                "extensions": [],
                "graphics": {},
                "javaScript": {},
                "modifiedPreferences": {
                    "print.macosx.pagesetup": "QWERTY",
                    "print.macosx.pagesetup-2": "POIUYT"
                },
                "userJS": {
                    "exists": false
                }
            }'''}

    def setUp(self):
        super(AAQTemplateTestCase, self).setUp()

        self.user = user(save=True)
        self.client.login(username=self.user.username, password='testpass')

    def _post_new_question(self, locale=None):
        """Post a new question and return the response."""
        p = product(title='Firefox', slug='firefox', save=True)
        for l in QuestionLocale.objects.all():
            p.questions_locales.add(l)
        topic(slug='fix-problems', product=p, save=True)
        extra = {}
        if locale is not None:
            extra['locale'] = locale
        url = urlparams(
            reverse('questions.aaq_step5', args=['desktop', 'fix-problems'],
                    **extra),
            search='A test question')

        # Set 'in-aaq' for the session. It isn't already set because this
        # test doesn't do a GET of the form first.
        s = self.client.session
        s['in-aaq'] = True
        s.save()

        return self.client.post(url, self.data, follow=True)

    def test_full_workflow(self):
        response = self._post_new_question()
        eq_(200, response.status_code)
        assert 'Done!' in pq(response.content)('ul.user-messages li').text()

        # Verify question is in db now
        question = Question.objects.filter(title='A test question')[0]

        # Make sure question is in questions list
        response = self.client.get(reverse('questions.list', args=['all']))
        doc = pq(response.content)
        eq_(1, len(doc('#question-{0!s}'.format(question.id))))
        # And no email was sent
        eq_(0, len(mail.outbox))

        # Verify product and topic assigned to question.
        eq_('fix-problems', question.topic.slug)
        eq_('firefox', question.product.slug)

        # Verify troubleshooting information
        troubleshooting = question.metadata['troubleshooting']
        assert 'modifiedPreferences' in troubleshooting
        assert 'print.macosx' not in troubleshooting

        # Verify firefox version
        version = question.metadata['ff_version']
        eq_('18.0.2', version)

    def test_localized_creation(self):
        response = self._post_new_question(locale='pt-BR')
        eq_(200, response.status_code)
        assert 'Done!' in pq(response.content)('ul.user-messages li').text()

        # Verify question is in db now
        question = Question.objects.filter(title='A test question')[0]
        eq_(question.locale, 'pt-BR')

    def test_full_workflow_inactive(self):
        u = self.user
        u.is_active = False
        u.save()
        RegistrationProfile.objects.create_profile(u)
        response = self._post_new_question()
        eq_(200, response.status_code)

        # Verify question is in db now
        question = Question.objects.filter(title='A test question')[0]

        # Make sure question is not in questions list
        response = self.client.get(reverse('questions.list', args=['all']))
        doc = pq(response.content)
        eq_(0, len(doc('li#question-{0!s}'.format(question.id))))
        # And no confirmation email was sent (already sent on registration)
        eq_(0, len(mail.outbox))

    def test_invalid_type(self):
        """Providing an invalid type returns 400."""
        p = product(slug='firefox', save=True)
        l = QuestionLocale.objects.get(locale=settings.LANGUAGE_CODE)
        p.questions_locales.add(l)
        topic(slug='fix-problems', product=p, save=True)
        self.client.logout()

        url = urlparams(
            reverse('questions.aaq_step5', args=['desktop', 'fix-problems']),
            search='A test question')
        # Register before asking question
        data = {'username': 'testaaq',
                'password': 'testpass', 'password2': 'testpass',
                'email': 'testaaq@example.com'}
        data.update(**self.data)
        response = self.client.post(url, data, follow=True)
        eq_(400, response.status_code)
        assert 'Request type not recognized' in response.content

    @mock.patch.object(Site.objects, 'get_current')
    def test_register_through_aaq(self, get_current):
        """Registering through AAQ form sends confirmation email."""
        get_current.return_value.domain = 'testserver'
        p = product(slug='firefox', save=True)
        l = QuestionLocale.objects.get(locale=settings.LANGUAGE_CODE)
        p.questions_locales.add(l)
        topic(slug='fix-problems', product=p, save=True)
        self.client.logout()
        title = 'A test question'
        url = urlparams(
            reverse('questions.aaq_step5', args=['desktop', 'fix-problems']),
            search=title)
        # Register before asking question
        data = {'register': 'Register', 'username': 'testaaq',
                'password': 'testpass1', 'password2': 'testpass1',
                'email': 'testaaq@example.com'}
        data.update(**self.data)
        self.client.post(url, data, follow=True)

        # Confirmation email is sent
        eq_(1, len(mail.outbox))
        eq_(mail.outbox[0].subject,
            'Please confirm your Firefox Help question')
        assert mail.outbox[0].body.find('?reg=aaq') > 0

        # Finally post question
        self.client.post(url, self.data, follow=True)

        # Verify question is in db now
        question = Question.objects.filter(title=title)
        eq_(1, question.count())
        eq_('testaaq', question[0].creator.username)

        # And no confirmation email was sent (already sent on registration)
        # Note: there was already an email sent above
        eq_(1, len(mail.outbox))

    def test_invalid_product_404(self):
        url = reverse('questions.aaq_step2', args=['lipsum'])
        response = self.client.get(url)
        eq_(404, response.status_code)

    def test_invalid_category_302(self):
        product(slug='firefox', save=True)
        url = reverse('questions.aaq_step3', args=['desktop', 'lipsum'])
        response = self.client.get(url)
        eq_(302, response.status_code)

    def test_no_aaq_link_in_header(self):
        """Verify the ASK A QUESTION link isn't present in header."""
        p = product(slug='firefox', save=True)
        l = QuestionLocale.objects.get(locale=settings.LANGUAGE_CODE)
        p.questions_locales.add(l)
        url = reverse('questions.aaq_step2', args=['desktop'])
        response = self.client.get(url)
        eq_(200, response.status_code)
        assert '/questions/new' not in pq(response.content)('#aux-nav').html()


class ProductForumTemplateTestCase(TestCaseBase):
    def test_product_forum_listing(self):
        firefox = product(title='Firefox', slug='firefox', save=True)
        android = product(title='Firefox for Android', slug='mobile',
                          save=True)
        fxos = product(title='Firefox OS', slug='firefox-os',
                       save=True)
        openbadges = product(title='Open Badges', slug='open-badges',
                             save=True)

        l = QuestionLocale.objects.get(locale=settings.LANGUAGE_CODE)
        firefox.questions_locales.add(l)
        android.questions_locales.add(l)
        fxos.questions_locales.add(l)

        response = self.client.get(reverse('questions.home'))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(4, len(doc('.product-list .product')))
        product_list_html = doc('.product-list').html()
        assert firefox.title in product_list_html
        assert android.title in product_list_html
        assert fxos.title in product_list_html
        assert openbadges.title not in product_list_html


class RelatedThingsTestCase(ElasticTestCase):
    def setUp(self):
        super(RelatedThingsTestCase, self).setUp()
        self.question = question(title='lorem ipsum',
                                 content='lorem',
                                 product=product(save=True),
                                 save=True)

    def test_related_questions(self):
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('#related-content .related-question')))

        q1 = question(title='lorem ipsum dolor',
                      content='lorem',
                      product=self.question.product,
                      save=True)
        a1 = answer(question=q1, save=True)
        answervote(answer=a1, helpful=True, save=True)

        # Questions with no helpful answers should not be shown
        q2 = question(title='lorem ipsum dolor',
                      content='lorem',
                      product=self.question.product,
                      save=True)
        answer(question=q2, save=True)

        # Questions that belong to different products should not be shown
        q3 = question(title='lorem ipsum dolor',
                      content='lorem',
                      product=product(save=True),
                      save=True)
        a3 = answer(question=q3, save=True)
        answervote(answer=a3, helpful=True, save=True)

        cache.clear()
        self.refresh()

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('#related-content .related-question')))

    def test_related_documents(self):
        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(0, len(doc('#related-content .related-document')))

        d1 = document(title='lorem ipsum', save=True)
        d1.products.add(self.question.product)
        r1 = revision(document=d1, summary='lorem',
                      content='lorem ipsum dolor',
                      is_approved=True, save=True)
        d1.current_revision = r1
        d1.save()

        cache.clear()
        self.refresh()

        response = get(self.client, 'questions.details',
                       args=[self.question.id])
        doc = pq(response.content)
        eq_(1, len(doc('#related-content .related-document')))
