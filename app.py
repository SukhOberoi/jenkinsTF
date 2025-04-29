# mock_oauth_server.py
# A simple mock OAuth2 server for Google Home Cloud-to-Cloud Account Linking (dev/test only)

from flask import Flask, request, redirect, session, render_template_string, jsonify
import requests
import json
import secrets
import time
from threading import Lock
import os

TOKENS_FILE = "tokens.json"

def save_tokens_to_file():
    with open(TOKENS_FILE, "w") as f:
        json.dump({
            "access_tokens": access_tokens,
            "refresh_tokens": refresh_tokens
        }, f)

def load_tokens_from_file():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            data = json.load(f)
            return data.get("access_tokens", {}), data.get("refresh_tokens", {})
    return {}, {}

# Load existing tokens on startup
access_tokens, refresh_tokens = load_tokens_from_file()

# Prevent duplicate triggers
execution_lock = Lock()
last_trigger_time = 0
MIN_INTERVAL = 10  # seconds

app = Flask(__name__)
app.secret_key = secrets.token_urlsafe(16)

# In-memory stores

auth_codes = {}   # code -> (client_id, redirect_uri)

LOGIN_PAGE = '''
<!doctype html>
<title>Mock OAuth Login</title>
<h2>Mock OAuth Login</h2>
<form method="post">
  <input type="hidden" name="client_id" value="{{client_id}}">
  <input type="hidden" name="redirect_uri" value="{{redirect_uri}}">
  <input type="hidden" name="state" value="{{state}}">
  <p><label>Username: <input type="text" name="username"></label></p>
  <p><label>Password: <input type="password" name="password"></label></p>
  <p><button type="submit">Login</button></p>
</form>
'''

@app.route('/authorize', methods=['GET', 'POST'])
def authorize():
    if request.method == 'GET':
        client_id = request.args.get('client_id')
        redirect_uri = request.args.get('redirect_uri')
        state = request.args.get('state', '')
        if client_id not in VALID_CLIENTS:
            return "Unknown client_id", 400
        return render_template_string(LOGIN_PAGE, client_id=client_id, redirect_uri=redirect_uri, state=state)

    client_id = request.form.get('client_id')
    redirect_uri = request.form.get('redirect_uri')
    state = request.form.get('state', '')
    username = request.form.get('username')
    password = request.form.get('password')

    code = secrets.token_urlsafe(8)
    auth_codes[code] = (client_id, redirect_uri)

    separator = '&' if '?' in redirect_uri else '?'
    return redirect(f"{redirect_uri}{separator}code={code}&state={state}")

@app.route('/token', methods=['POST'])
def token():
    grant_type = request.form.get('grant_type')

    if grant_type == 'authorization_code':
        code = request.form.get('code')
        redirect_uri = request.form.get('redirect_uri')
        client_id = request.form.get('client_id')
        client_secret = request.form.get('client_secret')

        if VALID_CLIENTS.get(client_id) != client_secret:
            return jsonify(error='invalid_client'), 401

        stored = auth_codes.pop(code, None)
        if not stored or stored[0] != client_id or stored[1] != redirect_uri:
            return jsonify(error='invalid_grant'), 400

        access_token = secrets.token_urlsafe(16)
        refresh_token = secrets.token_urlsafe(24)

        access_tokens[access_token] = client_id
        refresh_tokens[refresh_token] = client_id
        save_tokens_to_file()

        return jsonify(
            access_token=access_token,
            token_type='Bearer',
            expires_in=3600,
            refresh_token=refresh_token
        )

    elif grant_type == 'refresh_token':
        refresh_token = request.form.get('refresh_token')
        client_id = request.form.get('client_id')
        client_secret = request.form.get('client_secret')

        if VALID_CLIENTS.get(client_id) != client_secret:
            return jsonify(error='invalid_client'), 401

        if refresh_tokens.get(refresh_token) != client_id:
            return jsonify(error='invalid_grant'), 400

        new_token = secrets.token_urlsafe(16)
        access_tokens[new_token] = client_id
        save_tokens_to_file()

        return jsonify(
            access_token=new_token,
            token_type='Bearer',
            expires_in=3600
        )

    return jsonify(error='unsupported_grant_type'), 400


# ----- JENKINS WEBHOOK CONFIG -----

device_states = {'jenkins_job': False}

def trigger_jenkins(action):
    global last_trigger_time
    with execution_lock:
        current_time = time.time()
        if current_time - last_trigger_time < MIN_INTERVAL:
            print("Duplicate trigger ignored due to cooldown.")
            return False

        last_trigger_time = current_time
        params = {
            'token': JENKINS_TOKEN,
            'action': action,
            'autoApprove': 'true'
        }
        print(f"Sending request to Jenkins with params: {params}")
        try:
            resp = requests.post(JENKINS_WEBHOOK_URL, params=params,
                                 auth=(JENKINS_USER, JENKINS_API_TOKEN), timeout=5)
            return resp.status_code == 200
        except requests.RequestException as e:
            print(f"Jenkins trigger failed: {e}")
            return False

@app.route('/smarthome', methods=['POST'])
def smarthome():
    data   = request.get_json()
    req_id = data['requestId']
    intent = data['inputs'][0]['intent']

    if intent == 'action.devices.SYNC':
        return jsonify({
            'requestId': req_id,
            'payload': {
                'agentUserId': 'user-1234',
                'devices': [
                    {
                        'id': 'jenkins_job',
                        'type': 'action.devices.types.SWITCH',
                        'traits': ['action.devices.traits.OnOff'],
                        'name': {
                            'defaultNames': ['Terraform Job'],
                            'name': 'Jenkins Apply',
                            'nicknames': ['terraform']
                        },
                        'willReportState': True
                    }
                ]
            }
        })

    if intent == 'action.devices.QUERY':
        devices = data['inputs'][0]['payload']['devices']
        resp_devices = {}
        for d in devices:
            did = d['id']
            on_state = device_states.get(did, False)
            resp_devices[did] = {'online': True, 'on': on_state}
        return jsonify({
            'requestId': req_id,
            'payload': {'devices': resp_devices}
        })

    if intent == 'action.devices.EXECUTE':
        results = []
        for cmd in data['inputs'][0]['payload']['commands']:
            for dev in cmd['devices']:
                did = dev['id']
                params = cmd['execution'][0]['params']
                onoff  = params.get('on', False)
                device_states[did] = onoff
                success = trigger_jenkins('apply' if onoff else 'destroy')
                results.append({
                    'ids': [did],
                    'status': 'SUCCESS' if success else 'ERROR',
                    'states': {
                        'on': onoff,
                        'online': True
                    }
                })
        return jsonify({
            'requestId': req_id,
            'payload': {'commands': results}
        })

    return jsonify({
        'requestId': req_id,
        'payload': {'errorCode': 'unsupported_intent'}
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
