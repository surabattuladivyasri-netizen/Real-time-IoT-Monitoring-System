from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for, flash, send_file, render_template
import datetime
import os
import json
from flask_sqlalchemy import SQLAlchemy
from collections import defaultdict
import threading
import socket
import sys
import pandas as pd
import io
import datetime
from sqlalchemy import desc
import queue
import logging
import time

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
                                                            
# --- CONFIGURATION ---
app.secret_key = os.urandom(24)
app.permanent_session_lifetime = datetime.timedelta(minutes=5)
CONFIG_FILE = 'config.json'
ALARM_EVENTS_FILE = 'alarm_events_log.json'
ADMIN_CREDENTIALS = {"username": "admin", "password": "password"}

CLIENT_OFFLINE_THRESHOLD_SECONDS = 10
BROADCAST_PORT = 9999
BROADCAST_MESSAGE = b'IOT_SERVER_DISCOVERY'

# Use a dictionary to track last known GPIO states for alarm detection
app.last_gpio_states = defaultdict(lambda: [-1] * 8)
app.gpio_lock = threading.Lock()
log_queue = queue.Queue()

# --- Database Configuration ---
# Use the correct user, password, and database name.
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:PostgreSQL@localhost:5432/iotdb'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class AlarmProcessorState(db.Model):
    """
    This table will only ever have ONE row.
    Its job is to store the ID of the last row from the 'readings' table
    that the background worker has successfully processed.
    """
    __tablename__ = 'alarm_processor_state'
    id = db.Column(db.Integer, primary_key=True) # A fixed ID, e.g., 1
    last_processed_reading_id = db.Column(db.Integer, default=0, nullable=False)

    def __repr__(self):
        return f'<AlarmProcessorState last_id: {self.last_processed_reading_id}>'

class AlarmEvents(db.Model):
    __tablename__ = 'alarm_events'  # The new table name
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(80), nullable=False)
    pin_index = db.Column(db.Integer, nullable=False)
    event_start_time = db.Column(db.DateTime, nullable=False, default=datetime.datetime.now)
    event_end_time = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<AlarmEvents Client: {self.client_id}, Pin: {self.pin_index}>'

class Readings(db.Model):
    __tablename__ = 'readings'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(80), nullable=False)
    created_at = db.Column('created_at', db.DateTime(timezone=True), default=datetime.datetime.now)

    temp0 = db.Column(db.Float)
    temp1 = db.Column(db.Float)
    temp2 = db.Column(db.Float)
    temp3 = db.Column(db.Float)
    temp4 = db.Column(db.Float)
    temp5 = db.Column(db.Float)
    temp6 = db.Column(db.Float)
    temp7 = db.Column(db.Float)
    
    gpio0 = db.Column(db.Integer)
    gpio1 = db.Column(db.Integer)
    gpio2 = db.Column(db.Integer)
    gpio3 = db.Column(db.Integer)
    gpio4 = db.Column(db.Integer)
    gpio5 = db.Column(db.Integer)
    gpio6 = db.Column(db.Integer)
    gpio7 = db.Column(db.Integer)
    
    hum0 = db.Column(db.Float)
    hum1 = db.Column(db.Float)
    hum2 = db.Column(db.Float)
    hum3 = db.Column(db.Float)
    hum4 = db.Column(db.Float)
    hum5 = db.Column(db.Float)
    hum6 = db.Column(db.Float)
    hum7 = db.Column(db.Float)

    def __repr__(self):
        return f'<Readings {self.client_id} at {self.created_at}>'
    

# --- CONFIG FILE HANDLING ---
def load_config():
    defaults = {
        "port": 5000,
        "visible_gpio_pins": {},
        "client_aliases": {},
        "gpio_aliases": {},
        "i2c_aliases": {},
        "visible_i2c_sensors": {},
        "hum_aliases": {},
        "visible_hum_sensors": {}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(defaults, f, indent=4)
        return defaults
    try:
        with open(CONFIG_FILE, 'r') as f:
            config_data = json.load(f)
            for key, value in defaults.items():
                config_data.setdefault(key, value)
            return config_data
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not load config.json, reverting to defaults. Error: {e}")
        return defaults

def save_config(config_data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=4)

app_config = load_config()
app.known_client_ids = set()

# --- Server Discovery Listener ---
def discovery_listener():
    """Listens for client broadcast messages and responds with an acknowledgment."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        try:
            sock.bind(('', BROADCAST_PORT))
            print(f"[DISCOVERY] Server listening for discovery on UDP port {BROADCAST_PORT}")
        except OSError as e:
            print(f"[ERROR] Failed to bind discovery port: {e}. Is another process running? Exiting.")
            os._exit(1)

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                if data == BROADCAST_MESSAGE:
                    print(f"[DISCOVERY] Received discovery message from {addr[0]}. Responding...")
                    sock.sendto(b'DISCOVERY_ACK', addr)
            except Exception as e:
                print(f"[DISCOVERY] Listener error: {e}")

# --- Create database tables if they don't exist ---
with app.app_context():
    db.create_all()

def check_database_connection():
    """
    A one-time test to run at startup to confirm database connectivity and table access.
    """
    logging.info("--- [DB CHECK] Running Database Connection Test ---")
    try:
        with app.app_context():
            # 1. Test Reading
            readings_count = Readings.query.count()
            logging.info(f"--- [DB CHECK] SUCCESS: Can connect and read from 'readings' table. Found {readings_count} rows.")
            
            alarms_count = AlarmEvents.query.count()
            logging.info(f"--- [DB CHECK] SUCCESS: Can connect and read from 'alarm_events' table. Found {alarms_count} rows.")
            
            # 2. Test Writing and Deleting
            logging.info("--- [DB CHECK] Testing WRITE access to 'alarm_events'...")
            test_event = AlarmEvents(
                client_id='_db_connection_test_',
                pin_index=99,
                event_start_time=datetime.datetime.now(datetime.timezone.utc)
            )
            db.session.add(test_event)
            db.session.commit()
            logging.info("--- [DB CHECK] SUCCESS: Wrote a temporary test record.")
            
            db.session.delete(test_event)
            db.session.commit()
            logging.info("--- [DB CHECK] SUCCESS: Deleted the temporary test record.")
            logging.info("--- [DB CHECK] Database Connection Test Passed! ---")

    except Exception as e:
        logging.error("--- [DB CHECK] FAILED: Could not complete the database connection test.", exc_info=True)


def background_alarm_processor():
    """
    This is the new core of your alarm logic. It runs in a continuous loop,
    watches the 'readings' table for new data added by Node-RED, and
    updates the 'alarm_events' table when it finds a completed alarm.
    """
    logging.info("[AlarmProcessor] Background worker started.")
    
    # One-time setup: Make sure the state tracker row exists.
    with app.app_context():
        if not AlarmProcessorState.query.first():
            logging.info("[AlarmProcessor] First-time run: Initializing state tracker in the database.")
            initial_state = AlarmProcessorState(id=1, last_processed_reading_id=0)
            db.session.add(initial_state)
            db.session.commit()

    while True:
        try:
            with app.app_context():
                state = AlarmProcessorState.query.get(1)
                last_processed_id = state.last_processed_reading_id
                new_readings = Readings.query.filter(Readings.id > last_processed_id).order_by(Readings.id.asc()).all()

                if new_readings:
                    logging.info(f"[AlarmProcessor] Waking up. Found {len(new_readings)} new rows from Node-RED.")
                    
                    for reading in new_readings:
                        previous_reading = Readings.query.filter(
                            Readings.client_id == reading.client_id,
                            Readings.id < reading.id
                        ).order_by(Readings.id.desc()).first()

                        if previous_reading:
                            for pin_index in range(8):
                                current_state = getattr(reading, f'gpio{pin_index}')
                                previous_state = getattr(previous_reading, f'gpio{pin_index}')

                                if current_state == 0 and previous_state == 1:
                                    logging.info(f"  [AlarmProcessor] Found ALARM END on Pin {pin_index} for '{reading.client_id}'. Calculating duration...")
                                    end_time = reading.created_at
                                    start_time = None
                                    gpio_col = getattr(Readings, f'gpio{pin_index}')
                                    
                                    last_safe_reading = Readings.query.filter(Readings.client_id == reading.client_id, Readings.created_at < previous_reading.created_at, gpio_col == 0).order_by(desc(Readings.created_at)).first()
                                    if last_safe_reading:
                                        first_alarm_reading = Readings.query.filter(Readings.client_id == reading.client_id, Readings.created_at > last_safe_reading.created_at, gpio_col == 1).order_by(Readings.created_at.asc()).first()
                                        if first_alarm_reading:
                                            start_time = first_alarm_reading.created_at
                                    else:
                                        first_ever_reading = Readings.query.filter(Readings.client_id == reading.client_id, gpio_col == 1).order_by(Readings.created_at.asc()).first()
                                        if first_ever_reading:
                                            start_time = first_ever_reading.created_at
                                    
                                    # --- ADDED THIS ELSE BLOCK FOR BETTER LOGGING ---
                                    if start_time:
                                        new_event = AlarmEvents(client_id=reading.client_id, pin_index=pin_index, event_start_time=start_time, event_end_time=end_time)
                                        db.session.add(new_event)
                                        logging.info(f"  [AlarmProcessor] SUCCESS: Staged 'alarm_events' record for Pin {pin_index}.")
                                    else:
                                        logging.warning(f"  [AlarmProcessor] Could not determine a start time for the alarm on Pin {pin_index}. No record will be created for this event.")
                                    # --- END OF ADDITION ---
                    
                    latest_processed_id = new_readings[-1].id
                    state.last_processed_reading_id = latest_processed_id
                    logging.info(f"[AlarmProcessor] Finished batch. Updating last processed ID to {latest_processed_id}.")
                    db.session.commit()

            time.sleep(10)
        except Exception as e:
            logging.error(f"[AlarmProcessor] FATAL ERROR in background worker: {e}", exc_info=True)
            db.session.rollback()
            time.sleep(30)


@app.route('/data')
def get_dashboard_data():
    filtered_data = {}
    
    all_known_client_ids = db.session.query(Readings.client_id).distinct().all()
    all_known_client_ids = [cid[0] for cid in all_known_client_ids]

    for client_id in all_known_client_ids:
        latest_entry = Readings.query.filter_by(client_id=client_id).order_by(desc(Readings.created_at)).first()
        is_connected = False
        
        if latest_entry:
            time_since_last_update = (datetime.datetime.now(datetime.timezone.utc) - latest_entry.created_at.replace(tzinfo=datetime.timezone.utc)).total_seconds()
            if time_since_last_update < CLIENT_OFFLINE_THRESHOLD_SECONDS:
                is_connected = True

        client_info = {}
        client_info['display_name'] = app_config['client_aliases'].get(client_id, client_id)
        client_info['is_connected'] = is_connected

        if is_connected:
            client_info['timestamp'] = latest_entry.created_at.strftime("%Y-%m-%d %H:%M:%S")
            
            i2c_aliases = app_config.get('i2c_aliases', {}).get(client_id, {})
            hum_aliases = app_config.get('hum_aliases', {}).get(client_id, {})
            gpio_aliases = app_config.get('gpio_aliases', {}).get(client_id, {})

            # --- TEMPERATURE AND HUMIDITY LOGIC ---
            client_info['combined_sensors'] = []
            for channel in range(8):
                temp = getattr(latest_entry, f'temp{channel}', None)
                hum = getattr(latest_entry, f'hum{channel}', None)
                if temp is not None or hum is not None:
                    temp_alias = i2c_aliases.get(str(channel), f"Sensor {channel}")
                    hum_alias = hum_aliases.get(str(channel), f"Humidity {channel}")
                    is_visible = channel in app_config.get('visible_i2c_sensors', {}).get(client_id, list(range(8))) or \
                                 channel in app_config.get('visible_hum_sensors', {}).get(client_id, list(range(8)))
                    if is_visible:
                        client_info['combined_sensors'].append({
                            'channel': channel,
                            'display_name': temp_alias,
                            'temperature': temp,
                            'humidity': hum
                        })

            # --- GPIO LOGIC ---
            client_info['gpio_pins'] = []
            client_info['gpio_statuses'] = []
            client_info['gpio_aliases'] = []
            client_info['gpio_alarm_logs'] = {}
            
            visible_pins_set = set(app_config.get('visible_gpio_pins', {}).get(client_id, list(range(8))))
            for pin_index in range(8):
                if pin_index in visible_pins_set:
                    gpio_status = getattr(latest_entry, f'gpio{pin_index}', None)
                    if gpio_status is not None:
                        client_info['gpio_pins'].append(pin_index)
                        client_info['gpio_statuses'].append(gpio_status)
                        alias = gpio_aliases.get(str(pin_index), f"GPIO {pin_index}")
                        client_info['gpio_aliases'].append(alias)
                        
                        start_time, end_time = None, None
                        is_active = False
                        gpio_col = getattr(Readings, f'gpio{pin_index}')

                        if gpio_status == 1:
                            is_active = True
                            last_safe_reading = Readings.query.filter(
                                Readings.client_id == client_id,
                                Readings.created_at <= latest_entry.created_at,
                                gpio_col == 0
                            ).order_by(desc(Readings.created_at)).first()

                            if last_safe_reading:
                                first_alarm_reading = Readings.query.filter(
                                    Readings.client_id == client_id,
                                    Readings.created_at > last_safe_reading.created_at,
                                    gpio_col == 1
                                ).order_by(Readings.created_at.asc()).first()
                                if first_alarm_reading:
                                    start_time = first_alarm_reading.created_at
                            else:
                                first_ever_reading = Readings.query.filter(
                                    Readings.client_id == client_id,
                                    gpio_col == 1
                                ).order_by(Readings.created_at.asc()).first()
                                if first_ever_reading:
                                    start_time = first_ever_reading.created_at
                        else:
                            is_active = False
                            last_alarm_reading = Readings.query.filter(
                                Readings.client_id == client_id,
                                Readings.created_at <= latest_entry.created_at,
                                gpio_col == 1
                            ).order_by(desc(Readings.created_at)).first()

                            if last_alarm_reading:
                                end_time = last_alarm_reading.created_at
                                last_safe_reading = Readings.query.filter(
                                    Readings.client_id == client_id,
                                    Readings.created_at < end_time,
                                    gpio_col == 0
                                ).order_by(desc(Readings.created_at)).first()

                                if last_safe_reading:
                                    first_alarm_reading = Readings.query.filter(
                                        Readings.client_id == client_id,
                                        Readings.created_at > last_safe_reading.created_at,
                                        gpio_col == 1
                                    ).order_by(Readings.created_at.asc()).first()
                                    if first_alarm_reading:
                                        start_time = first_alarm_reading.created_at
                                else:
                                    first_ever_reading = Readings.query.filter(
                                        Readings.client_id == client_id,
                                        gpio_col == 1
                                    ).order_by(Readings.created_at.asc()).first()
                                    if first_ever_reading:
                                        start_time = first_ever_reading.created_at
                        
                        if start_time:
                            client_info['gpio_alarm_logs'][str(pin_index)] = {
                                "start_time": start_time.strftime('%Y-%m-%d %H:%M:%S'),
                                "end_time": end_time.strftime('%Y-%m-%d %H:%M:%S') if end_time else None,
                                "is_active": is_active
                            }
        
        filtered_data[client_id] = client_info

    return jsonify(filtered_data)


# --- ADMIN & AUTHENTICATION ROUTES ---
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if 'logged_in' in session:
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        if request.form['username'] == ADMIN_CREDENTIALS['username'] and request.form['password'] == ADMIN_CREDENTIALS['password']:
            session['logged_in'], session.permanent = True, True
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials. Please try again.', 'error')
    return render_template('login.html')

@app.route('/admin/dashboard', methods=['GET', 'POST'])
def admin_dashboard():
    if 'logged_in' not in session:
        return redirect(url_for('admin_login'))

    all_known_client_ids = db.session.query(Readings.client_id).distinct().all()
    all_known_client_ids = [cid[0] for cid in all_known_client_ids]

    if request.method == 'POST':
        if new_port := request.form.get('port', type=int):
            if app_config['port'] != new_port:
                app_config['port'] = new_port
                flash('Server port updated. Please restart the server for the change to take effect.', 'warning')

        for client_id in all_known_client_ids:
            alias_value = request.form.get(f'alias_{client_id}')
            if alias_value:
                app_config['client_aliases'][client_id] = alias_value
            elif client_id in app_config['client_aliases']:
                del app_config['client_aliases'][client_id]

            visible_pins_str = request.form.getlist(f'gpio_visible_{client_id}', type=str)
            visible_pins = [int(pin) for pin in visible_pins_str]
            app_config['visible_gpio_pins'][client_id] = visible_pins
            
            for pin_index in range(8):
                alias_value = request.form.get(f'gpio_alias_{client_id}_{pin_index}')
                if alias_value:
                    app_config['gpio_aliases'].setdefault(client_id, {})[str(pin_index)] = alias_value
                elif client_id in app_config['gpio_aliases'] and str(pin_index) in app_config['gpio_aliases'][client_id]:
                    del app_config['gpio_aliases'][client_id][str(pin_index)]

            visible_i2c_sensors_str = request.form.getlist(f'i2c_visible_{client_id}', type=str)
            visible_i2c_sensors = [int(s) for s in visible_i2c_sensors_str]
            app_config['visible_i2c_sensors'][client_id] = visible_i2c_sensors
            
            for channel_index in range(8):
                alias_value = request.form.get(f'i2c_alias_{client_id}_{channel_index}')
                if alias_value:
                    app_config['i2c_aliases'].setdefault(client_id, {})[str(channel_index)] = alias_value
                elif client_id in app_config['i2c_aliases'] and str(channel_index) in app_config['i2c_aliases'][client_id]:
                    del app_config['i2c_aliases'][client_id][str(channel_index)]

            visible_hum_sensors_str = request.form.getlist(f'hum_visible_{client_id}', type=str)
            visible_hum_sensors = [int(s) for s in visible_hum_sensors_str]
            app_config['visible_hum_sensors'][client_id] = visible_hum_sensors
            
            for channel_index in range(8):
                alias_value = request.form.get(f'hum_alias_{client_id}_{channel_index}')
                if alias_value:
                    app_config['hum_aliases'].setdefault(client_id, {})[str(channel_index)] = alias_value
                elif client_id in app_config['hum_aliases'] and str(channel_index) in app_config['hum_aliases'][client_id]:
                    del app_config['hum_aliases'][client_id][str(channel_index)]
        
        save_config(app_config)
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('admin_dashboard'))
    
    clients_with_data = {}
    for client_id in all_known_client_ids:
        latest_entry = Readings.query.filter_by(client_id=client_id).order_by(Readings.id.desc()).first()
        is_connected = False
        client_data = {
            "timestamp": "N/A",
            "combined_sensors": [],
            "gpio_statuses": []
        }

        if latest_entry:
            entry_timestamp = latest_entry.created_at
            time_since_last_update = (datetime.datetime.now(datetime.timezone.utc) - entry_timestamp).total_seconds()
            if time_since_last_update < CLIENT_OFFLINE_THRESHOLD_SECONDS:
                is_connected = True
                client_data["timestamp"] = entry_timestamp.strftime("%Y-%m-%d %H:%M:%S")

                for i in range(8):
                    temp = getattr(latest_entry, f'temp{i}', None)
                    hum = getattr(latest_entry, f'hum{i}', None)
                    if temp is not None or hum is not None:
                        client_data['combined_sensors'].append({
                            'channel': i,
                            'temperature': temp,
                            'humidity': hum
                        })
                
                for i in range(8):
                    gpio = getattr(latest_entry, f'gpio{i}', None)
                    if gpio is not None:
                        client_data['gpio_statuses'].append(gpio)
        
        clients_with_data[client_id] = client_data
    
    return render_template('admin.html', clients=clients_with_data, config=app_config)


@app.route('/admin/view_database', methods=['GET'])
def view_database():
    if 'logged_in' not in session:
        return redirect(url_for('admin_login'))

    selected_client_id = request.args.get('client_id')
    selected_table = request.args.get('table', 'readings')

    unique_client_ids = db.session.query(Readings.client_id).distinct().all()
    unique_client_ids = [cid[0] for cid in unique_client_ids]

    db_entries = []

    if selected_table == 'alarm_events':
        query = AlarmEvents.query
        if selected_client_id and selected_client_id != 'all':
            query = query.filter_by(client_id=selected_client_id)
        all_entries = query.order_by(AlarmEvents.event_start_time.desc()).limit(1000).all()
        for entry in all_entries:
            db_entries.append({
                "id": entry.id,
                "client_id": entry.client_id,
                "pin_index": entry.pin_index,
                "start_time": entry.event_start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": entry.event_end_time.strftime("%Y-%m-%d %H:%M:%S") if entry.event_end_time else "Active"
            })
    else: # Default to 'readings'
        query = Readings.query
        if selected_client_id and selected_client_id != 'all':
            query = query.filter_by(client_id=selected_client_id)
        all_entries = query.order_by(Readings.id.desc()).limit(1000).all()
        for entry in all_entries:
            row_data = {
                "id": entry.id,
                "client_id": entry.client_id,
                "timestamp": entry.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for i in range(8):
                row_data[f'gpio{i}'] = getattr(entry, f'gpio{i}', None)
                row_data[f'temp{i}'] = getattr(entry, f'temp{i}', None)
                row_data[f'hum{i}'] = getattr(entry, f'hum{i}', None)
            db_entries.append(row_data)

    return render_template('database.html', db_entries=db_entries, unique_client_ids=unique_client_ids, selected_client_id=selected_client_id, selected_table=selected_table)


@app.route('/admin/export_excel', methods=['POST'])
def export_excel():
    if 'logged_in' not in session:
        return redirect(url_for('admin_login'))

    start_time_str = request.form.get('from_time')
    end_time_str = request.form.get('to_time')
    client_id = request.form.get('client_id')
    table = request.form.get('table')

    try:
        start_time = datetime.datetime.fromisoformat(start_time_str) if start_time_str else datetime.datetime.min
        end_time = datetime.datetime.fromisoformat(end_time_str) if end_time_str else datetime.datetime.max
        
        data = None
        if table == 'alarm_events':
            query = AlarmEvents.query.filter(AlarmEvents.event_start_time.between(start_time, end_time))
            if client_id and client_id != 'all': query = query.filter_by(client_id=client_id)
            results = query.all()

            if not results:
                flash('No data found for the selected criteria.', 'error')
                return redirect(url_for('view_database', client_id=client_id, table=table))
            
            # --- FIX APPLIED HERE ---
            data = pd.DataFrame([
                {
                    "ID": r.id, "Client ID": r.client_id, "Pin Index": r.pin_index, 
                    "Event Start Time": r.event_start_time.replace(tzinfo=None), 
                    "Event End Time": r.event_end_time.replace(tzinfo=None) if r.event_end_time else None
                } for r in results
            ])
            # --- END OF FIX ---

        else: # Default to readings
            query = Readings.query.filter(Readings.created_at.between(start_time, end_time))
            if client_id and client_id != 'all': query = query.filter_by(client_id=client_id)
            results = query.all()

            if not results:
                flash('No data found for the selected criteria.', 'error')
                return redirect(url_for('view_database', client_id=client_id, table=table))
            
            data_rows = []
            for entry in results:
                # --- FIX APPLIED HERE ---
                row = {
                    "ID": entry.id, 
                    "Client ID": entry.client_id, 
                    "Timestamp": entry.created_at.replace(tzinfo=None) # This removes the timezone
                }
                # --- END OF FIX ---
                for i in range(8): row[f"GPIO {i}"] = getattr(entry, f'gpio{i}', None)
                for i in range(8): row[f"I2C CH {i}"] = getattr(entry, f'temp{i}', None)
                for i in range(8): row[f"HUM {i}"] = getattr(entry, f'hum{i}', None)
                data_rows.append(row)
            data = pd.DataFrame(data_rows)
        
        if data is not None and not data.empty:
            data = data.fillna('N/A')
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                data.to_excel(writer, index=False, sheet_name=table)
            output.seek(0)
            filename = f"{table}_export_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
            return send_file(output, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            flash('No data found for the selected criteria.', 'error')
            return redirect(url_for('view_database', client_id=client_id, table=table))

    except Exception as e:
        # Create a more user-friendly error message for this specific issue
        if "Excel does not support datetimes with timezones" in str(e):
             flash('Excel export failed due to a timezone issue. Please try again or contact support if the problem persists.', 'error')
        else:
            flash(f'An unexpected error occurred during export: {e}', 'error')
        return redirect(url_for('view_database', client_id=client_id, table=table))
    


@app.route('/admin/logout')
def admin_logout():
    session.pop('logged_in', None)
    return redirect(url_for('admin_login'))


# --- GRAPHING & TIME-LAPSE ROUTES ---
@app.route('/graphs')
def graphs():
    all_known_client_ids = db.session.query(Readings.client_id).distinct().all()
    all_known_client_ids = [cid[0] for cid in all_known_client_ids]
    return render_template('graphs.html', clients=all_known_client_ids)


@app.route('/graph_data')
def graph_data():
    end_time_str = request.args.get('timestamp')
    if end_time_str:
        end_time = datetime.datetime.fromisoformat(end_time_str)
    else:
        end_time = datetime.datetime.now(datetime.timezone.utc)

    final_graph_data = defaultdict(lambda: {'timestamps': [], 'i2c_data': defaultdict(list), 'gpio_data': defaultdict(list), 'hum_data': defaultdict(list)})
    start_time = end_time - datetime.timedelta(minutes=15)

    all_known_client_ids = db.session.query(Readings.client_id).distinct().all()
    all_known_client_ids = [cid[0] for cid in all_known_client_ids]

    for client_id in all_known_client_ids:
        client_alias = app_config['client_aliases'].get(client_id, client_id)
        
        query = Readings.query.filter(
            Readings.client_id == client_id,
            Readings.created_at.between(start_time, end_time)
        ).order_by(Readings.created_at.asc()).all()

        if not query:
            continue

        timestamps = [entry.created_at.isoformat() for entry in query]
        final_graph_data[client_alias]['timestamps'] = timestamps

        for entry in query:
            for i in range(8):
                temp = getattr(entry, f'temp{i}', None)
                if temp is not None:
                    aliases = app_config.get('i2c_aliases', {}).get(client_id, {})
                    alias = aliases.get(str(i), f"Sensor {i}")
                    final_graph_data[client_alias]['i2c_data'][alias].append(temp)
            
            for i in range(8):
                hum = getattr(entry, f'hum{i}', None)
                if hum is not None:
                    aliases = app_config.get('hum_aliases', {}).get(client_id, {})
                    alias = aliases.get(str(i), f"Humidity {i}")
                    final_graph_data[client_alias]['hum_data'][alias].append(hum)

            for i in range(8):
                gpio_status = getattr(entry, f'gpio{i}', None)
                if gpio_status is not None:
                    aliases = app_config.get('gpio_aliases', {}).get(client_id, {})
                    alias = aliases.get(str(i), f"GPIO {i}")
                    final_graph_data[client_alias]['gpio_data'][alias].append(gpio_status)

    return jsonify(final_graph_data)

# --- MAIN DASHBOARD ROUTE ---
@app.route('/')
def dashboard():
    return render_template('dashboard.html')



if __name__ == "__main__":
    # Start the discovery listener in a background thread
    discovery_thread = threading.Thread(target=discovery_listener, daemon=True)
    discovery_thread.start()
    logging.info("UDP Discovery listener started.")
    
    # Start the background alarm processor thread
    alarm_processor_thread = threading.Thread(target=background_alarm_processor, daemon=True)
    alarm_processor_thread.start()

    # Run the Flask app in the main thread
    # This is the standard way to run a Flask app.
    port = app_config.get('port', 5000)
    logging.info(f"Flask dashboard server starting on port {port}.")
    app.run(host="0.0.0.0", port=port, debug=False)