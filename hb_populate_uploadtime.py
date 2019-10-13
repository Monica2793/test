#!/home/ubuntu/miniconda3/envs/hb_py37/bin/python

"""
Populates the HB_PANEL_UPLOAD_METRICS table with the new panel upload time of past 3 months
"""

# MODULES #
from __future__ import print_function
import argparse
import json
import logging
import os
import socket
import sys
import traceback
import csv
import datetime
from pprint import pprint
from decimal import Decimal
from decimal import getcontext
from collections import OrderedDict
import requests
import pytz
import pymysql
import boto3
import botocore
from plumbum.cmd import gunzip, wc, rm

SCRIPT_NAME = os.path.basename(sys.argv[0])
logger = logging.getLogger(SCRIPT_NAME)
LOG_FORMAT = '%(asctime)s:[%(name)s]:[%(module)s - %(funcName)s]:[%(levelname)s]: %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S %Z'
logger.setLevel(logging.DEBUG)
line_break = "=========================================================================================================================================="

#set the timezone to PST
tz = pytz.timezone('America/Los_Angeles')

boto3_session = boto3.Session()

#dbconfig
db_config = {}
hb_config = {}
panels_to_update_transaction_count = []

##used for raising custom exceptions
class CustomValueError(ValueError):
    def __init__(self, arg):
        self.strerror = arg
        self.args = {arg}
		
#######################################################################################
# HELPER FUNCTIONS
#######################################################################################

def setup_argparse():
    """set up argparse"""
    ## By default, if '-R' parameter is not passed, start command will not be issued
    parser = argparse.ArgumentParser(description="Populates the HB_PANEL_UPLOAD_METRICS table with the new panel upload time of past 3 months")
    parser.add_argument('-j', '--json', help='JSON config file [<config>.json]',\
             required=True)
    parser.add_argument('-d', "--debug", help="Enables debug mode", action='store_true')
    return parser.parse_args()
	
def get_console_handler(log_level, log_format, date_format):
    """returns a console handler"""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    formatter = logging.Formatter(log_format, date_format)
    console_handler.setFormatter(formatter)
    return console_handler
	
def load_params(filename):
    """Loads a set of parameters provided a filename"""
    if isinstance(filename, str):
        input_file = open(filename)
        params = json.loads(input_file.read())
        input_file.close()
        return params
    return filename

def get_config(config_file):
    """Returns CONFIG dictionary. Gets corresponding JSON, Schema JSON files & validates"""
    logger.info("Received config file '{0}'".format(config_file))
    return load_params(config_file)
	
def run_update_query(query, data):
    '''run update query with data'''
    conn = pymysql.connect(user=db_config['dbUser'], passwd=db_config['dbPassword'], host=db_config['dbHost'], port=db_config['dbPort'], db=db_config['dbName'])
    cursor = conn.cursor()
    cursor.execute(query, data)
    conn.commit()
    cursor.close()
    conn.close()	

def get_panels_containers(panels):
    '''Get list of panels and their containers'''
    panel_containers=[]
	new_panels=['CPANELV3', '4MV4']

    for panel in panels:
		if panel in new_panels:
			panel_containers.append((panel, "BANK"))
			panel_containers.append((panel, "CARD"))

    return panel_containers

def update_panel_transaction_count_for_uploaded_panels():
    '''for each panel that been uploaded within the last window
    get the transaction count and update the count in DB'''
    for panel in panels_to_update_transaction_count:
        panel["record_count"] = insert_panel_transaction_count(panel["panel_name"], panel["container"], panel["s3_key"], panel["bucket_name"], panel["panel_date"])


def insert_panel_transaction_count(panel, container, s3_key, bucket_name, panel_date):
    '''update the panel transaction count in HB_PANEL_UPLOAD_METRICS table'''
    s3_resource = boto3_session.resource('s3')
    try:
        s3_object = s3_resource.Object(bucket_name, s3_key)
        
        panel_local_filename = hb_config["hb_s3_path"]["localfile_prefix"] + panel_date + hb_config["panel_config"][panel][container]["s3_key_suffix"]

        logger.info("Downloading {0} panel file - {1}".format(panel, panel_local_filename))
        s3_object.download_file(panel_local_filename)

        #Command to find the transaction count
        transaction_count_cmd = gunzip["-c",panel_local_filename] | wc["-l"]
        transaction_count = transaction_count_cmd().rstrip('\n')
        
        logger.info("Transcation count of {0} - {1}".format(panel_local_filename, transaction_count))

        #Remove the panel file in local filesystem after getting the transaction count
        remove_panel_file = rm[panel_local_filename] 
        remove_panel_file()

        logger.info("{0} {1} record count - {2}".format(panel, container, transaction_count))

        insert_record_count_sql = '''UPDATE HB_PANEL_UPLOAD_TIMES 
        SET NUMBER_OF_RECORDS= %s
        WHERE PANEL= %s
        AND CONTAINER= %s
        AND PANEL_DATE= %s
        '''

        query_data = (transaction_count, panel, container, panel_date)

        logger.debug("Query to update the record count - {0}".format(insert_record_count_sql))

        run_update_query(insert_record_count_sql, query_data)

        return transaction_count
#######################################################################################
# MAIN PROGRAM
#######################################################################################
def main():
    """main program"""
	global db_config
    global hb_config
    global panels_to_update_transaction_count
	
	try:
        #read command line arguments
        args = setup_argparse()

        if(args.debug == True):
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO

        #setup logger
        console_handler = get_console_handler(log_level, LOG_FORMAT, DATE_FORMAT)
        logger.addHandler(console_handler)
        logger.info(line_break)

        #read config from json
        hb_config = get_config(args.json)
        db_config = hb_config["hb_db"]
	
	    #get upload times of the panels for 92 days and populate the database
		panels_containers = get_panels_containers(hb_config["panel_config"]["panels"])
		for panel, container in panels_containers: 
			s3_resource = boto3_session.resource('s3')

			bucket_name = hb_config["panel_config"][panel]["bucket_name"]
			for n in range(1, 93):
				pt_n = datetime.datetime.now(tz) - datetime.timedelta(days=n)
				panel_date = pt_n.strftime('%Y%m%d')
				s3_key = hb_config["panel_config"][panel][container]["s3_key_prefix"] + "/" + panel_date +  hb_config["panel_config"][panel][container]["s3_key_suffix"]

				s3_object = s3_resource.Object(bucket_name, s3_key)

				panel_upload_time = s3_object.last_modified.astimezone(tz)
				logger.debug("File %s last modified on %s", s3_key, panel_upload_time)

				insert_panel_upload_time_query = "INSERT INTO HB_PANEL_UPLOAD_TIMES (PANEL,CONTAINER,PANEL_DATE,UPLOAD_TIME) VALUES (%s, %s, %s, %s)"
				logger.debug("SQL query to insert the panel upload time - %s", insert_panel_upload_time_query)
				logger.debug("PANEL - {0} CONTAINER - {1} PANEL DATE - {2} PANEL_UPLOAD_DATE - {3}".format(panel, container, panel_date, panel_upload_time.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')))
				query_data = (panel, container, panel_date, panel_upload_time.strftime('%Y-%m-%d %H:%M:%S'))
				run_update_query(insert_panel_upload_time_query, query_data)

				panels_to_update_transaction_count.append({
					"panel_name": panel, 
					"container": container,
					"s3_key": s3_key,
					"bucket_name": bucket_name,
					"panel_date": panel_date})
					
		#update panel transaction count
		if panels_to_update_transaction_count:
			#Update the transaction count in DB
			update_panel_transaction_count_for_uploaded_panels()
			

if __name__ == '__main__':
    main()