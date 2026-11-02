"""Survey polls are answered through their survey, not the poll URL."""

import unittest
from unittest.mock import Mock

from autopollev.voter import Voter, VoteError
from autopollev.auth import CookieExpiredError


class FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        # The survey results endpoint answers with a list of results.
        self.payload = [{"id": 1, "option_id": 7}] if payload is None else payload
        self.text = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, response, own_results):
        self.response = response
        self.own_results = own_results
        self.posts = []

    def get(self, url, **kwargs):
        return FakeResponse(200, self.own_results)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class FakeAuth:
    def __init__(self, response=None, own_results=()):
        self.session = FakeSession(response or FakeResponse(), list(own_results))

    @staticmethod
    def get_csrf_token():
        return "csrf"

    @staticmethod
    def check_response_status(_response):
        return True


class FakeHistory:
    def record_vote(self, **_kwargs):
        pass


# A survey poll as get_poll_options returned it live on 2 Nov: its
# participant_key is not its permalink.
SURVEY_POLL = {"id": 83442780, "title": "Highly leveraged",
               "participant_key": "tXHleNyFoVvRUpQ0QX7IX"}


class SurveyVoteTests(unittest.TestCase):
    def test_survey_poll_posts_to_the_survey_results_url(self):
        auth = FakeAuth()
        result = Voter(auth, FakeHistory())._submit_vote(
            "xJovpwnYhjYZM8kTuOuSo", 7, "Bank reserves.", SURVEY_POLL,
            survey_uid="x3OGh4BIQclN49wJ3bBaE",
        )

        url, kwargs = auth.session.posts[0]
        self.assertTrue(url.endswith("/surveys/x3OGh4BIQclN49wJ3bBaE/results"))
        self.assertEqual(
            [{
                "option_id": 7,
                # The permalink here was a 404 ResourceNotFound.
                "participant_key": "tXHleNyFoVvRUpQ0QX7IX",
                "activity_type": "multiple_choice_poll",
                "source": "activated_multi_page_survey",
            }],
            kwargs["json"],
        )
        self.assertEqual("success", result["status"])

    def test_lone_poll_still_posts_to_its_own_results_url(self):
        auth = FakeAuth()
        Voter(auth, FakeHistory())._submit_vote("solo", 7, "A", {"id": 1})

        url, kwargs = auth.session.posts[0]
        self.assertTrue(url.endswith("/multiple_choice_polls/solo/results"))
        self.assertEqual("pollev_page", kwargs["data"]["source"])

    def test_already_answered_poll_is_done_not_retried(self):
        # Live 422 after answering by hand on pollev.com.
        auth = FakeAuth(FakeResponse(422, {"error": {
            "type": "UnprocessableEntity",
            "message": "Your response was not submitted",
            "detail": [{"message": "You can no longer respond to this "
                        "activity: it allows up to 1 responses per participant"}],
        }}))
        result = Voter(auth, FakeHistory())._submit_vote("Irw5", 7, "A", {"id": 1})

        self.assertEqual("answered", result["status"])

    def test_answer_given_by_hand_during_the_dialog_is_never_overwritten(self):
        # Live 2 Nov: answered in the browser at 10:01:48 while the dialog
        # was open; the timeout fallback was posted at 10:02:12.
        for survey_uid in (None, "FXmx5TTCqCuoWqE9A9Qrh"):
            with self.subTest(survey_uid=survey_uid):
                auth = FakeAuth(own_results=[
                    {"poll_id": 1, "value": "another poll"},
                    {"poll_id": 83442780, "value": "both."},
                ])
                result = Voter(auth, FakeHistory())._submit_vote(
                    "xJovpwnYhjYZM8kTuOuSo", 7, "random", SURVEY_POLL, survey_uid)

                self.assertEqual({"status": "answered", "value": "both."}, result)
                self.assertEqual([], auth.session.posts)


class TextVoteTests(unittest.TestCase):
    def test_unverified_submission_is_blocked_without_network_access(self):
        for survey_uid in (None, 'survey'):
            with self.subTest(survey_uid=survey_uid):
                auth, history = Mock(), Mock()
                voter = Voter(auth, history)
                result = voter._submit_vote('text', None, 'Not sure', {'title': 'Question'},
                                            survey_uid, 'free_text_poll')
                self.assertEqual('blocked', result['status'])
                auth.get_csrf_token.assert_not_called()
                auth.session.post.assert_not_called()
                history.record_vote.assert_called_once_with(
                    poll_id='text', poll_title='Question', selected_option='Not sure',
                    selected_option_id=None, status='blocked', response_code=0,
                )

    def test_auto_text_uses_fallback_and_no_option_id(self):
        voter = Voter(Mock(), Mock())
        voter.get_poll_options = Mock(return_value={'title': 'Question', 'state': 'opened'})
        voter._submit_vote = Mock()
        voter.vote('text', survey_uid='survey', poll_type='free_text_poll')
        voter._submit_vote.assert_called_once_with(
            'text', None, 'Not sure', voter.get_poll_options.return_value,
            'survey', 'free_text_poll')

    def test_locked_text_poll_does_not_submit(self):
        voter = Voter(Mock(), Mock())
        voter.get_poll_options = Mock(return_value={'title': 'Question', 'state': 'closed'})
        voter._submit_vote = Mock()
        for method in (voter.vote, voter.interactive_vote):
            self.assertEqual('locked', method('text', poll_type='free_text_poll')['status'])
        voter._submit_vote.assert_not_called()

    def test_details_use_explicit_type_and_reject_unsupported_types(self):
        auth = Mock()
        auth.session.get.return_value.json.return_value = {'title': 'Question'}
        voter = Voter(auth, Mock())
        self.assertEqual({'title': 'Question'}, voter.get_poll_options('text', 'free_text_poll'))
        self.assertIn('/free_text_polls/text?', auth.session.get.call_args.args[0])
        with self.assertRaises(VoteError):
            voter.get_poll_options('text', '../unexpected')
        self.assertEqual(1, auth.session.get.call_count)

    def test_text_fetch_preserves_expired_session_error(self):
        auth = Mock()
        auth.check_response_status.side_effect = CookieExpiredError('expired')
        voter = Voter(auth, Mock())
        for method in (voter.vote, voter.interactive_vote):
            with self.assertRaises(CookieExpiredError):
                method('text', poll_type='free_text_poll')


if __name__ == "__main__":
    unittest.main()
