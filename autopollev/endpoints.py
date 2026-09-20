"""
PollEverywhere API endpoint definitions.

All endpoint URLs use str.format() placeholders that are filled in at runtime.
"""

ENDPOINTS = {
    # Home page
    'home': 'https://pollev.com/{host}',

    # CSRF token
    'csrf': 'https://pollev.com/proxy/api/csrf_token?_={timestamp}',

    # Username/password login (fallback)
    'login': 'https://pollev.com/proxy/api/sessions',

    # Firehose auth — get the firehose token and verify login status
    'firehose_auth': (
        'https://pollev.com/proxy/api/users/{host}/'
        'registration_info?_={timestamp}'
    ),

    # Logged-in account profile — identifies whose session cookie this is
    # (returns {"participant": {...}, "user": {...}} for the current cookie)
    'profile': (
        'https://pollev.com/proxy/api/profile?include=user&_={timestamp}'
    ),

    # Firehose polling — check the currently active poll (with token)
    'firehose_with_token': (
        'https://firehose-production.polleverywhere.com/users/{host}/activity/'
        'current.json?firehose_token={token}&last_message_sequence=0&_={timestamp}'
    ),

    # Firehose polling — check the currently active poll (no token)
    'firehose_no_token': (
        'https://firehose-production.polleverywhere.com/users/{host}/activity/'
        'current.json?last_message_sequence=0&_={timestamp}'
    ),

    # Get poll option details
    'poll_data': (
        'https://pollev.com/proxy/api/participant/'
        'multiple_choice_polls/{uid}?include=collection'
    ),

    # Submit a vote
    'respond_to_poll': (
        'https://pollev.com/proxy/api/participant/'
        'multiple_choice_polls/{uid}/results'
    ),

    # View submitted votes
    'check_responses': (
        'https://pollev.com/proxy/my/results?permalinks%5B%5D={uid}&'
        'per_page=500&include_archived=false&_={timestamp}'
    ),

    # Clear votes
    'clear_responses': 'https://pollev.com/proxy/api/results/{id}',
}
