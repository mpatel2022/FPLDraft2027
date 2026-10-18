import json
import pickle
import threading
import time

import pandas as pd
import pytest

import app

WAIT_SECONDS = 10


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b''):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves API responses rebuilt from the real season pickles."""

    def __init__(self, fail_on_players=False):
        self.fail_on_players = fail_on_players
        with open(app.DATA_DIR / 'metadata.pickle', 'rb') as handle:
            self.metadata = pickle.load(handle)
        with open(app.DATA_DIR / 'league_data.pickle', 'rb') as handle:
            self.league_details = pickle.load(handle)
        player_history = pd.read_pickle(app.DATA_DIR / 'player_history.pickle')
        self.history_by_player = {
            player_id: rows.rename(columns={'gameweek': 'event'}).to_dict('records')
            for player_id, rows in player_history.groupby('element')
        }
        _, completed_gameweeks = app.get_gameweeks(self.metadata)
        self.team_positions = pd.concat(
            pd.read_pickle(app.DATA_DIR / f'team_positions_history_gw{gameweek}.pickle')
            for gameweek in completed_gameweeks
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, timeout=None):
        if url == app.url_all:
            return FakeResponse(self.metadata)
        if '/element-summary/' in url:
            if self.fail_on_players:
                raise ConnectionError("network down")
            player_id = int(url.rsplit('/', 1)[1])
            return FakeResponse({'history': self.history_by_player.get(player_id, [])})
        if '/league/' in url:
            return FakeResponse(self.league_details)
        if '/entry/' in url:
            user_id, gameweek = (int(part) for part in url.split('/entry/')[1].split('/event/'))
            picks = self.team_positions[(self.team_positions['user_id'] == user_id)
                                        & (self.team_positions['gameweek'] == gameweek)]
            return FakeResponse({'picks': picks[['position', 'element']].to_dict('records')})
        return FakeResponse(content=b'image')


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect writes to a temp folder, keeping the config and real data readable."""
    data_dir = tmp_path / 'data'
    images_dir = tmp_path / 'assets'
    data_dir.mkdir()
    images_dir.mkdir()
    fake_session = FakeSession()
    expected = app.load_data()
    monkeypatch.setattr(app, 'DATA_DIR', data_dir)
    monkeypatch.setattr(app, 'IMAGES_LOCATION', f'{images_dir}/')
    monkeypatch.setattr(app.requests, 'Session', lambda: fake_session)
    return fake_session, data_dir, expected


@pytest.fixture(autouse=True)
def restore_app_state():
    original_data = {
        'next_gameweek': app.next_gameweek,
        'merged_df': app.merged_df,
        'user_options': app.user_options,
        'gameweek_options': app.gameweek_options,
    }
    original_state = app.get_refresh_state()
    yield
    app.apply_data(original_data)
    with app.refresh_lock:
        app.refresh_state.clear()
        app.refresh_state.update(original_state)


def wait_for_refresh_to_finish():
    deadline = time.monotonic() + WAIT_SECONDS
    while app.get_refresh_state()['running']:
        assert time.monotonic() < deadline, "Refresh did not finish"
        time.sleep(0.05)


def test_fetch_then_load_matches_current_data(sandbox):
    _, data_dir, expected = sandbox

    app.fetch_data()

    assert (data_dir / 'metadata.pickle').exists()
    rebuilt = app.load_data()
    pd.testing.assert_frame_equal(rebuilt['merged_df'], expected['merged_df'], check_like=True)
    assert rebuilt['gameweek_options'] == expected['gameweek_options']
    assert rebuilt['user_options'] == expected['user_options']


def test_failed_fetch_writes_no_pickles(sandbox):
    fake_session, data_dir, _ = sandbox
    fake_session.fail_on_players = True

    with pytest.raises(ConnectionError):
        app.fetch_data()

    assert not list(data_dir.glob('*.pickle'))


def test_refresh_runs_in_background_while_app_stays_usable(monkeypatch):
    release_fetch = threading.Event()
    fetch_started = threading.Event()

    def slow_fetch(report_progress=None):
        fetch_started.set()
        report_progress("Fetching players 25/700")
        release_fetch.wait(WAIT_SECONDS)

    new_data = app.load_data()
    new_data['merged_df'] = new_data['merged_df'].head(10)
    monkeypatch.setattr(app, 'fetch_data', slow_fetch)
    monkeypatch.setattr(app, 'load_data', lambda: new_data)
    original_merged_df = app.merged_df
    starting_version = app.get_refresh_state()['version']

    assert app.start_refresh() is True
    assert fetch_started.wait(WAIT_SECONDS)

    # A second click while running does not start another refresh
    assert app.start_refresh() is False
    state = app.get_refresh_state()
    assert state['running'] is True
    assert state['message'] == "Fetching players 25/700"

    # Callbacks still serve the existing data while the refresh is in progress
    assert app.merged_df is original_merged_df
    assert app.update_standings_view("table") is not None

    release_fetch.set()
    wait_for_refresh_to_finish()

    state = app.get_refresh_state()
    assert state['version'] == starting_version + 1
    assert state['message'].startswith("Updated")
    assert app.merged_df is new_data['merged_df']


def test_failed_refresh_keeps_existing_data(monkeypatch):
    def failing_fetch(report_progress=None):
        raise ConnectionError("network down")

    monkeypatch.setattr(app, 'fetch_data', failing_fetch)
    original_merged_df = app.merged_df
    starting_version = app.get_refresh_state()['version']

    assert app.start_refresh() is True
    wait_for_refresh_to_finish()

    state = app.get_refresh_state()
    assert state['version'] == starting_version
    assert state['message'] == "Refresh failed - see logs"
    assert app.merged_df is original_merged_df


def post_refresh_callback(client, triggered_prop, current_version):
    payload = {
        'output': '..refresh-status.children...refresh-poll.disabled...refresh-button.disabled...data-version.data..',
        'outputs': [
            {'id': 'refresh-status', 'property': 'children'},
            {'id': 'refresh-poll', 'property': 'disabled'},
            {'id': 'refresh-button', 'property': 'disabled'},
            {'id': 'data-version', 'property': 'data'},
        ],
        'inputs': [
            {'id': 'refresh-button', 'property': 'n_clicks', 'value': 1},
            {'id': 'refresh-poll', 'property': 'n_intervals', 'value': 1},
        ],
        'state': [{'id': 'data-version', 'property': 'data', 'value': current_version}],
        'changedPropIds': [triggered_prop],
    }
    response = client.post('/_dash-update-component', data=json.dumps(payload),
                           content_type='application/json')
    assert response.status_code in (200, 204)
    return response.get_json()['response'] if response.status_code == 200 else {}


def test_refresh_callback_flow(monkeypatch):
    release_fetch = threading.Event()
    monkeypatch.setattr(app, 'fetch_data', lambda report_progress=None: release_fetch.wait(WAIT_SECONDS))
    monkeypatch.setattr(app, 'load_data', lambda: {
        'next_gameweek': app.next_gameweek,
        'merged_df': app.merged_df,
        'user_options': app.user_options,
        'gameweek_options': app.gameweek_options,
    })
    client = app.server.test_client()
    starting_version = app.get_refresh_state()['version']

    # Clicking starts polling and disables the button, without new data yet
    clicked = post_refresh_callback(client, 'refresh-button.n_clicks', starting_version)
    assert clicked['refresh-poll'] == {'disabled': False}
    assert clicked['refresh-button'] == {'disabled': True}
    assert 'data-version' not in clicked

    release_fetch.set()
    wait_for_refresh_to_finish()

    # The next poll stops polling and bumps the version, which re-renders the tab
    polled = post_refresh_callback(client, 'refresh-poll.n_intervals', starting_version)
    assert polled['refresh-poll'] == {'disabled': True}
    assert polled['refresh-button'] == {'disabled': False}
    assert polled['data-version'] == {'data': starting_version + 1}
    assert polled['refresh-status']['children'].startswith("Updated")
