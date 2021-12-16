import threading
from queue import Queue
import requests
from requests import cookies
from bs4 import BeautifulSoup
from lxml import html
from credentials import credentials as creds
from utilities import utilities as utl
from tinydb import TinyDB, Query
from datetime import datetime, timedelta
import time


class RemoteDisplay:
    def __init__(self):
        self.query = Query()
        self.database_lock = threading.Lock()
        self.serial_queue = Queue()
        self.progress_counter_queue = Queue()
        self.database = TinyDB('project/databases/database.json')
        self.serial_list = []
        self.temp_list = []  # Used for storing values from units
        self.project = ''
        self.gateway_timeouts = 0
        self.unauthorized = False
        self.page_number = ''
        self.values = {}  # Values to be updated
        self.url = ""

        self.jar = requests.cookies.RequestsCookieJar()
        for parameter in creds.DISPLAY_COOKIES:
            self.jar.set(parameter[0], parameter[1], domain=parameter[2])
        self.session = requests.Session()

    def set_page(self, client, unit_serial):
        tree = html.fromstring(client.content)

        error = tree.xpath("//div[@class='text-danger d-inline-block']/text()")
        if len(error) > 0 or client.status_code != requests.codes.ok:
            if error[0] == 'Unauthorized':
                unauthorized = True
            self.update_database(unit_serial, 'failed', error)
            return True

        self.page_number = tree.xpath(
            "//li[(. = 'Temperature Control Config')]//a/@href | //td[(. = 'Temperature Control Config')]//a/@href")
        if len(self.page_number) == 0:
            self.update_database(unit_serial, 'failed', 'Failed to find Temperature Control Config page')
            return True

        self.page_number = self.page_number[0][-1:]
        return False

    def connect_to_page(self, unit_serial):
        try:
            client = self.session.get(f"https://{self.url}/{unit_serial}/private/config?"
                                      f"set={self.page_number}",
                                      cookies=self.jar,
                                      timeout=30)
            return client
        except requests.ConnectionError as exception:
            self.gateway_timeouts += 1
            self.update_database(unit_serial, 'failed', f'{exception}')
            return False
        except requests.RequestException as exception:
            self.update_database(unit_serial, 'failed', f'{exception}')
            return False

    def get_values(self, unit_serial):
        client = self.connect_to_page(unit_serial)
        if client is False:
            return False

        error = self.set_page(client, unit_serial)
        if error:
            return False

        client = self.connect_to_page(unit_serial)

        form_data = {}
        soup = BeautifulSoup(client.text, 'html.parser')
        options = soup.find_all('option', selected=True)
        selectors = soup.find_all('select')
        inputs = soup.find_all('input')

        options_count = len(options)
        selectors_count = len(selectors)
        
        if options_count != selectors_count:
            self.update_database(unit_serial, 'failed', f'Selectors {selectors_count}, Options: {options_count}')
            return False

        for option_selection, selector in zip(options, selectors):
            name = selector.get('name')
            value = option_selection.get('value')
            form_data[name] = value
        for input_field in inputs:
            name = input_field.get('name')
            value = input_field.get('value')
            form_data[name] = value
        return form_data

    @staticmethod
    def target_values(form_data):
        correct_values = form_data['Fan1'] == '100' and \
                         form_data['Fan2'] == '100'
                         
        return True if correct_values else False

    def validate_values(self, unit_serial):
        new_values = {'Fan1': '100', 'Fan2': '100'}
        current_values = self.get_values(unit_serial)

        fields_exists = new_values.keys() <= current_values.keys()
        if not fields_exists:
            missing_fields = new_values.keys() - current_values.keys()
            self.update_database(unit_serial, 'failed', f'Missing fields: {missing_fields}')
            return False
        else:
            values_match = new_values.items() <= current_values.items()
            if not values_match:
                mismatched_values = dict(new_values.items() - current_values.items())
                message = ', '.join("{}={}".format(k, v) for (k, v) in mismatched_values.items())
                self.update_database(unit_serial, 'failed', f'Mismatched values: {message}')
                return False
        return True

    def connect_to_unit(self, unit_serial):
        form_data = self.get_values(unit_serial)
        if form_data is False:
            return

        valid_values = self.validate_values(unit_serial)
        if not valid_values:
            return

        if not all(key in form_data for key in (
                'Fan1',
                'Fan2'
        )):
            self.update_database(unit_serial, 'failed', 'Missing required fields')
            return

        if self.target_values(form_data):
            self.update_database(unit_serial, 'succeeded')
            return
        else:
            self.update_database(unit_serial, 'unprocessed')

        form_data['Fan1'] = '100'
        form_data['Fan2'] = '100'

        try:
            foo = self.session.post(f"https://{self.url}/{unit_serial}/private/config?set={self.page_number}",
                              data=form_data,
                              timeout=120)

        except Exception as exception:
            self.update_database(unit_serial, 'failed', f'Form POST failed: {exception}')
            return

        attempts = 1
        time.sleep(10)

        while True:
            form_data = self.get_values(unit_serial)
            if not form_data:
                self.update_database(unit_serial, 'failed', 'Field did not update')
                return
            if self.target_values(form_data):
                self.update_database(unit_serial, 'succeeded')
                break
            if attempts == 6:
                self.update_database(unit_serial, 'failed', 'Field did not update after 5 attempts')

                return
            attempts += 1
            time.sleep(5)

        data = {'RestartServer': 'Restart+Server+Only'}
        while True:
            client = session.post(f'https://{self.url}/{unit_serial}/settings?set=100',
                                  data=data, timeout=60)
            print(f"{unit_serial} {utl.get_time('timestamp')} \n")
            print(f"{client.content}")
            print("\r")

    def update_database(self, serial, status, note=''):
        with self.database_lock:
            self.database.table(self.project).update({'status': f'{status}',
                                                      'note': f'{note}',
                                                      'timestamp': f'{datetime.now()}'},
                                                     self.query.serial == f'{serial}')

    def process_serial(self):
        while True:
            try:
                serial = self.serial_queue.get()
                self.progress_counter_queue.put(1)
                self.connect_to_unit(serial)
            except Exception as exception:
                print(exception)
            self.serial_queue.task_done()

    @staticmethod
    def thread_maker(target):
        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()

    def progress_counter(self):
        serial_count = len(self.serial_list)
        processed_count = 0
        while True:
            processed_count += self.progress_counter_queue.get()
            print("\r" + f"started: {processed_count}/{serial_count}", end="")
            self.progress_counter_queue.task_done()
            if processed_count == serial_count:
                break
        print('\n\n' + 'waiting on unit response...', end='\r')

    def processor(self):
        records = self.database.table(self.project).all()
        if len(records) == 0:
            print('no records found' + '\n')
            return

        for record in records:
            if record['status'] == 'unprocessed' or record['status'] == 'failed':
                self.serial_list.append(record['serial'])

        self.thread_maker(self.progress_counter)

        start = time.time()

        for serial in self.serial_list:
            self.thread_maker(self.process_serial)
            self.serial_queue.put(serial)

        self.serial_queue.join()

        print('' + f'entire job took: {round(time.time() - start, 2)} seconds' + '\n')

        with open(f"project/logs/{self.project}_{datetime.now().strftime('%m-%d-%Y_%H.%M.%S')}.txt", "a") as file:
            statuses = ['unprocessed', 'succeeded', 'failed']
            succeeded_count = ''
            status_list = []

            for status in statuses:
                status_count = len(self.database.table(self.project).search(self.query.status == status))
                record = self.database.table(self.project).search(self.query.status == status)
                if status_count > 0:
                    status_list.append(f'{status}')
                    status_list.extend(record)
                if status == 'succeeded':
                    succeeded_count = status_count
                utl.multi_print(f'{status}: {status_count}', file=file)

            if self.gateway_timeouts > 0:
                utl.multi_print(f'gateway timeouts: {self.gateway_timeouts}', file=file)
            if self.unauthorized:
                utl.multi_print(f'unauthorized: {self.unauthorized}', file=file)
            utl.multi_print(
                f'completed: {round(succeeded_count / len(self.database.table(self.project)) * 100, 2)}%' + '\n',
                file=file)

            for item in status_list:
                linebreak = ''
                colon = ''
                if item in statuses:
                    linebreak = '\n'
                    colon = ':'
                utl.multi_print(f'{linebreak}' + f'{item}{colon}', file=file, skip_console=True)

    def run(self):
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36'})
        while True:
            utl.clear_console()
            self.serial_list.clear()
            self.gateway_timeouts = 0
            self.page_number = ""
            self.project = ""
            self.processor()

            for second in range(60, 0, -1):
                print('\r' + f'Time until next run: {str(timedelta(seconds=second))[3:]}', end='')
                time.sleep(1)
