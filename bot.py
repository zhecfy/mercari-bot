import os
import sys
sys.path.append("Yoku")
import argparse
import logging
from typing import Tuple, List
from datetime import datetime

from mercari.mercari.mercari import MercariSort, MercariOrder, MercariSearchStatus, Item
from mercari.mercari.mercari import search as search_mercari

from Yoku.yoku.consts import KEY_TITLE, KEY_IMAGE, KEY_URL, KEY_POST_TIMESTAMP, KEY_END_TIMESTAMP, KEY_START_TIMESTAMP, KEY_ITEM_ID, KEY_BUYNOW_PRICE, KEY_CURRENT_PRICE, KEY_START_PRICE
from Yoku.yoku.scrape import search as search_yahoo_auctions

from email_utils import EmailConfig, send_tracking_email, prettify
from json_utils import load_file_to_json, save_json_to_file
from config import *

def update(entry: dict) -> Tuple[bool, List]:
    if "site" not in entry or entry["site"] == SITE_MERCARI: # for backwards compatibility
        if entry["level"] == LEVEL_ABSOLUTELY_UNIQUE or entry["level"] == LEVEL_UNIQUE:
            search_keyword = entry["keyword"]
        elif entry["level"] == LEVEL_AMBIGUOUS:
            search_keyword = entry["keyword"] + " " + entry["supplement"]
        else:
            raise ValueError("unknown level")

        success, search_result = search_mercari(search_keyword,
                                                sort=MercariSort.SORT_SCORE,
                                                order=MercariOrder.ORDER_DESC,
                                                status=MercariSearchStatus.DEFAULT,
                                                category_id=[entry["category_id"]],
                                                request_interval=REQUEST_INTERVAL)
        
        if not success:
            return False, []

        if entry["level"] == LEVEL_ABSOLUTELY_UNIQUE:
            filtered_search_result = search_result
        elif entry["level"] == LEVEL_UNIQUE or entry["level"] == LEVEL_AMBIGUOUS:
            filtered_search_result = []
            for item in search_result:
                if entry["keyword"].lower() in item.productName.lower():
                    filtered_search_result.append(item)
        
        return True, filtered_search_result
    elif entry["site"] == SITE_YAHOO_AUCTIONS:

        parameter_keys = ["p", "auccat", "brand_id", "aucmaxprice", "s1", "o1", "fixed"]
        parameters = {key: entry[key] for key in parameter_keys if key in entry}

        if "auccat" in parameters and parameters["auccat"] == 0:
            parameters.pop("auccat")

        search_result = search_yahoo_auctions(parameters, request_interval=REQUEST_INTERVAL)

        # assume yahoo auction searches always succeed
        # TODO: handle connection errors here
        return True, search_result
    else:
        raise ValueError("unknown site")

def add():
    # 1. read current track.json
    track_json = load_file_to_json(file_path=RESULT_PATH)
    if track_json == None:
        track_json = []

    max_entry_id = 0
    for track_entry in track_json:
        max_entry_id = max(max_entry_id, track_entry["id"])
    
    # 2. interactively add keyword
    new_entry = {}

    # id (unique)
    new_entry["id"] = max_entry_id + 1

    # site
    while True:
        site = input(f"site ('m' for {SITE_MERCARI}, 'y' for {SITE_YAHOO_AUCTIONS}): ")
        if site == "m":
            new_entry["site"] = SITE_MERCARI
            break
        elif site == "y":
            new_entry["site"] = SITE_YAHOO_AUCTIONS
            break
        else:
            print("site error")
            continue

    # keyword (mercari) or p (yahoo_auctions)
    if new_entry["site"] == SITE_MERCARI:
        new_entry["keyword"] = input("search keyword: ")
    elif new_entry["site"] == SITE_YAHOO_AUCTIONS:
        new_entry["p"] = input("search keyword: ")

    # level (mercari only)
    if new_entry["site"] == SITE_MERCARI:
        while True:
            level = int(input("keyword's ambiguity level: "))
            if level == LEVEL_ABSOLUTELY_UNIQUE or level == LEVEL_UNIQUE:
                new_entry["level"] = level
                break
            elif level == LEVEL_AMBIGUOUS:
                new_entry["level"] = level
                new_entry["supplement"] = input("supplemental keyword: ")
                break
            else:
                print("level error")
                continue
    
    # category_id (mercari) or auccat (yahoo_auctions)
    if new_entry["site"] == SITE_MERCARI:
        new_entry["category_id"] = int(input(f"category_id of search (all: 0, CD: {MERCARI_CATEGORY_CD}): "))
    elif new_entry["site"] == SITE_YAHOO_AUCTIONS:
        new_entry["auccat"] = int(input(f"auccat of search (all: 0, Music: {YAHOO_CATEGORY_MUSIC}): "))
    
    # 3. initial update
    success, search_result = update(new_entry)
    if not success:
        print("initial update failed, abort")
        return
    search_result_dict = {}
    if new_entry["site"] == SITE_MERCARI:
        for item in search_result:
            search_result_dict[item.id] = {"price": item.price, "status": item.status}
    elif new_entry["site"] == SITE_YAHOO_AUCTIONS:
        for item in search_result:
            search_result_dict[item[KEY_ITEM_ID]] = {"price": item[KEY_CURRENT_PRICE], "endtime": item[KEY_END_TIMESTAMP]}
    new_entry["last_result"] = search_result_dict
    new_entry["last_time"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    # 4. write back to track.json
    track_json.append(new_entry)
    save_json_to_file(track_json, RESULT_PATH)
    return

def track():
    email_items = [] # list of tuple(entry, list of tuple(Item, status))
    # 1. read current track.json
    track_json = load_file_to_json(file_path=RESULT_PATH)
    if track_json == None:
        track_json = []
    new_track_json = []
    # 2. for each entry:
    for entry in track_json:
        email_entry_items = []
        # 2.1. update search result
        success, search_result = update(entry)
        if not success:
            logging.error(f"Update of {entry} failed, skipping")
            new_track_json.append(entry)
            continue
        # 2.2. compare with last result
        last_search_result_dict = entry["last_result"]
        search_result_dict = {}

        # site-specific actions
        if "site" not in entry or entry["site"] == SITE_MERCARI: # for backwards compatibility
            entry["site"] = SITE_MERCARI
            for item in search_result:
                search_result_dict[item.id] = {"price": item.price, "status": item.status}
                # 2.3. if anything new:
                if item.id not in last_search_result_dict: # New
                    email_entry_items.append((item, TRACK_STATUS_NEW))
                elif search_result_dict[item.id] != last_search_result_dict[item.id]: # Modified
                    modification = []
                    for key in search_result_dict[item.id]:
                        if search_result_dict[item.id][key] != last_search_result_dict[item.id][key]:
                            modification.append(prettify(key, last_search_result_dict[item.id][key]) + "->" + prettify(key, search_result_dict[item.id][key]))
                        # print(key, modification)
                    email_entry_items.append((item, TRACK_STATUS_MODIFIED + "(" + ", ".join(modification) + ")"))
        elif entry["site"] == SITE_YAHOO_AUCTIONS:
            for item in search_result:
                search_result_dict[item[KEY_ITEM_ID]] = {"price": item[KEY_CURRENT_PRICE], "endtime": item[KEY_END_TIMESTAMP]}
                if item[KEY_ITEM_ID] not in last_search_result_dict: # New
                    email_entry_items.append((item, TRACK_STATUS_NEW))
                elif search_result_dict[item[KEY_ITEM_ID]] != last_search_result_dict[item[KEY_ITEM_ID]]: # Modified
                    modification = []
                    for key in search_result_dict[item[KEY_ITEM_ID]]:
                        if search_result_dict[item[KEY_ITEM_ID]][key] != last_search_result_dict[item[KEY_ITEM_ID]][key]:
                            modification.append(prettify(key, last_search_result_dict[item[KEY_ITEM_ID]][key]) + "->" + prettify(key, search_result_dict[item[KEY_ITEM_ID]][key]))
                    email_entry_items.append((item, TRACK_STATUS_MODIFIED + "(" + ", ".join(modification) + ")"))

        entry["last_result"] = search_result_dict
        entry["last_time"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        new_track_json.append(entry)
        if len(email_entry_items) > 0:
            email_items.append((entry, email_entry_items))
    # 2.4. send email
    if len(email_items) > 0:
        send_tracking_email(EmailConfig(email_config_path=EMAIL_CONFIG_PATH), email_items)
    else:
        print("nothing new")
    
    # 3. write back to track.json
    save_json_to_file(new_track_json, RESULT_PATH)
    return

def list_():
    track_json = load_file_to_json(file_path=RESULT_PATH)
    if track_json == None:
        track_json = []
    for entry in track_json:
        print(prettify("entry", entry))

if __name__ == "__main__":
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(bot_dir)
    logging.basicConfig(filename="error.log", level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    parser = argparse.ArgumentParser(description="Yambot")
    parser.add_argument('action', choices=['add', 'list', 'track'])
    args = parser.parse_args()
    try:
        if args.action == 'add':
            add()
        elif args.action == "list":
            list_()
        elif args.action == 'track':
            track()
    except Exception as e:
        logging.error(f"An error occurred:\n{e}", exc_info=True)
