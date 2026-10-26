"""Survey polls are answered through their survey, not the poll URL."""

import unittest

from autopollev.voter import Voter


class FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        # The survey results endpoint answers with a list of results.
        self.payload = [{"id": 1, "option_id": 7}] if payload is None else payload
        self.text = ""

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class FakeAuth:
    def __init__(self, response=None):
        self.session = FakeSession(response or FakeResponse())

    @staticmethod
    def get_csrf_token():
        return "csrf"

    @staticmethod
    def check_response_status(_response):
        return True


class FakeHistory:
    def record_vote(self, **_kwargs):
        pass


class SurveyVoteTests(unittest.TestCase):
    def test_survey_poll_posts_to_the_survey_results_url(self):
        auth = FakeAuth()
        result = Voter(auth, FakeHistory())._submit_vote(
            "QUGYOkwvPQYmr6ZzgssoD", 7, "Bank reserves.", "Macro 4",
            survey_uid="x3OGh4BIQclN49wJ3bBaE",
        )

        url, kwargs = auth.session.posts[0]
        self.assertTrue(url.endswith("/surveys/x3OGh4BIQclN49wJ3bBaE/results"))
        self.assertEqual(
            [{
                "option_id": 7,
                "participant_key": "QUGYOkwvPQYmr6ZzgssoD",
                "activity_type": "multiple_choice_poll",
                "source": "activated_multi_page_survey",
            }],
            kwargs["json"],
        )
        self.assertEqual("success", result["status"])

    def test_lone_poll_still_posts_to_its_own_results_url(self):
        auth = FakeAuth()
        Voter(auth, FakeHistory())._submit_vote("solo", 7, "A", "Solo")

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
        result = Voter(auth, FakeHistory())._submit_vote("Irw5", 7, "A", "Q")

        self.assertEqual("answered", result["status"])


if __name__ == "__main__":
    unittest.main()
