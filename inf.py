import glob
import json
import os
import time
import psutil
import subprocess
import re
import websocket
import uuid
from git import Repo
from constants import APP_PORT, DEBUG_LOG_ENABLED, MODEL_DOWNLOAD_PATH_LIST, MODEL_FILETYPES, \
    OPTIONAL_MODELS, SERVER_ADDR
from utils.comfy.api import ComfyAPI
from utils.comfy.methods import ComfyMethod
from utils.common import clear_directory, copy_files, find_file_in_directory, find_process_by_port
from utils.file_downloader import ModelDownloader
from utils.logger import LoggingType, app_logger


class ComfyRunner:
    def __init__(self):
        self.comfy_api = ComfyAPI(SERVER_ADDR, APP_PORT)
        self.model_downloader = ModelDownloader(MODEL_DOWNLOAD_PATH_LIST)

    # TODO: create mixins for these kind of methods
    def is_server_running(self):
        pid = find_process_by_port(APP_PORT)
        return True if pid else False

    def start_server(self):
        # checking if comfy is already running
        if not self.is_server_running():
            command = "python ./ComfyUI/main.py"
            kwargs = {
                "shell" : True,
            }
            # TODO: remove comfyUI output from the console
            if not DEBUG_LOG_ENABLED:
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL

            self.server_process = subprocess.Popen(command, **kwargs)

            # waiting for server to start accepting requests
            while not self.is_server_running():
                time.sleep(0.5)

            app_logger.log(LoggingType.DEBUG, "comfy server is running")
        else:
            try:
                if not self.comfy_api.health_check():
                    raise Exception(f"Port {APP_PORT} blocked")
                else:
                    app_logger.log(LoggingType.DEBUG, "Server already running")
            except Exception as e:
                raise Exception(f"Port {APP_PORT} blocked")

    def stop_server(self):
        pid = find_process_by_port(APP_PORT)
        if pid:
            process = psutil.Process(pid)
            process.terminate()
            process.wait()

    def clear_comfy_logs(self):
        log_file_list = glob.glob("comfyui*.log")
        for file in log_file_list:
            if os.path.exists(file):
                os.remove(file)

    def get_output(self, ws, prompt, client_id, output_node_ids):
        prompt_id = self.comfy_api.queue_prompt(prompt, client_id)['prompt_id']

        # waiting for the execution to finish
        while True:
            out = ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if message['type'] == 'executing':
                    data = message['data']
                    if data['node'] is None and data['prompt_id'] == prompt_id:
                        break #Execution is done
            else:
                continue #previews are binary data

        # fetching results
        history = self.comfy_api.get_history(prompt_id)[prompt_id]
        output_list = {'file_list': [], 'text_output': []}
        for node_id in history['outputs']:
            if ((output_node_ids and len(output_node_ids) and int(node_id) in output_node_ids) or not output_node_ids):
                node_output = history['outputs'][node_id]
                print("node_output: ", node_output)
                if 'gifs' in node_output:
                    for gif in node_output['gifs']:
                        output_list['file_list'].append(gif['filename'])
                
                if 'text' in node_output:
                    for txt in node_output['text']:
                        output_list['text_output'].append(txt)

        return output_list

    def filter_missing_node(self, workflow):
        mappings = self.comfy_api.get_node_mapping_list()
        custom_node_list = self.comfy_api.get_all_custom_node_list()
        data = custom_node_list["custom_nodes"]

        # Build regex->url map
        regex_to_url = [
            {"regex": re.compile(item["nodename_pattern"]), "url": item["files"][0]}
            for item in data
            if item.get("nodename_pattern")
        ]

        # Build name->url map
        name_to_url = {
            name: url for url, names in mappings.items() for name in names[0]
        }

        registered_nodes = self.comfy_api.get_registered_nodes()

        missing_nodes = set()
        nodes = [node for _, node in workflow.items()]

        for node in nodes:
            node_type = node.get("class_type", "")
            if node_type.startswith("workflow/"):
                continue

            if node_type not in registered_nodes:
                url = name_to_url.get(node_type.strip(), "")
                if url:
                    missing_nodes.add(url)
                else:
                    for regex_item in regex_to_url:
                        if regex_item["regex"].search(node_type):
                            missing_nodes.add(regex_item["url"])

        unresolved_nodes = []   # not yet implemented in comfy

        for node_type in unresolved_nodes:
            url = name_to_url.get(node_type, "")
            if url:
                missing_nodes.add(url)

        ans = [node for node in data if any(file in missing_nodes for file in node.get("files", []))]
        # print("********* missing nodes found: ", ans)
        return ans

    def download_models(self, workflow, extra_models_list) -> dict:
        models_downloaded = False
        self.model_downloader.load_comfy_models()
        models_to_download = []

        for node in workflow:
            if "inputs" in workflow[node]:
                for input in workflow[node]["inputs"].values():
                    if isinstance(input, str) and any(
                        input.endswith(ft) for ft in MODEL_FILETYPES
                    ) and not any(input.endswith(m) for m in OPTIONAL_MODELS):
                        models_to_download.append(input)

        models_not_found = []
        for model in models_to_download:
            status, similar_models = self.model_downloader.download_model(model)
            if not status:
                models_not_found.append({
                    'model': model,
                    'similar_models': similar_models
                })
            else:
                models_downloaded = True

        for model in extra_models_list:
            status = self.model_downloader.download_file(model["filename"], model["url"], model["dest"])
            if status:
                models_downloaded = True
                for m in models_not_found:
                    if m['model'] == model['filename']:
                        models_not_found.remove(m)
                        break

        return {
            'data': {'models_not_found': models_not_found, 'models_downloaded': models_downloaded},
            'message': 'model(s) not found' if len(models_not_found) else '',
            'status': False if len(models_not_found) else True,
        }
    
    def download_custom_nodes(self, workflow, extra_node_urls) -> dict:
        nodes_installed = False

        # installing missing nodes
        missing_nodes = self.filter_missing_node(workflow)
        if len(missing_nodes):
            app_logger.log(LoggingType.INFO, f"Installing {len(missing_nodes)} custom nodes")
        for node in missing_nodes:
            app_logger.log(LoggingType.DEBUG, f"Installing {node['title']}")
            if node['installed'] in ['False', False]:
                nodes_installed = True
                status = self.comfy_api.install_custom_node(node)
                if status != {}:
                    app_logger.log(LoggingType.ERROR, "Failed to install custom node ", node["title"])

        # installing custom git repos
        if len(extra_node_urls):
            custom_node_list = self.comfy_api.get_all_custom_node_list()
            custom_node_list = custom_node_list["custom_nodes"]
            url_node_map = {}
            for node in custom_node_list:
                if node['reference'] not in url_node_map:
                    url_node_map[node['reference']] = [node]
                else:
                    url_node_map[node['reference']].append(node)

            for git_url in extra_node_urls:
                nodes_to_install = []
                if git_url in url_node_map:
                    for node in url_node_map[git_url]:
                        nodes_to_install.append(node)
                else:
                    node = {
                            'author': "",
                            'title': "",
                            'reference': git_url,
                            'files': [git_url],
                            'install_type': 'git-clone',
                            'description': "",
                            'installed': 'False'
                        }
                    nodes_to_install.append(node)
                
                for n in nodes_to_install:
                    nodes_installed = True
                    status = self.comfy_api.install_custom_node(n)
                    if status != {}:
                        app_logger.log(LoggingType.ERROR, "Failed to install custom node ", n["title"])

        return {
            'data': {'nodes_installed': nodes_installed},
            'message': '',
            'status': True
        }

    def load_workflow(self, workflow_input):
        if os.path.exists(workflow_input):
            try:
                with open(workflow_input, 'r') as file:
                    workflow_input = json.load(file)

            except Exception as e:
                app_logger.log(LoggingType.ERROR, "Exception: ", str(e))
                return None
        else:
            workflow_input = json.loads(workflow_input)

        return workflow_input if ComfyMethod.is_api_json(workflow_input) else None

    def predict(self, workflow_input, file_path_list=[], extra_models_list=[], extra_node_urls=[], stop_server_after_completion=False, clear_comfy_logs=True, output_folder="./output", output_node_ids=None):
        output_list = {}
        try:    
            # TODO: add support for image and normal json files
            workflow = self.load_workflow(workflow_input)
            if not workflow:
                app_logger.log(LoggingType.ERROR, "Invalid workflow file")
                return

            # cloning comfy repo
            app_logger.log(LoggingType.DEBUG, "cloning comfy repo")
            comfy_repo_url = "https://github.com/comfyanonymous/ComfyUI"
            comfy_manager_url = "https://github.com/ltdrdata/ComfyUI-Manager"
            if not os.path.exists("ComfyUI"):
                Repo.clone_from(comfy_repo_url, "ComfyUI")
            if not os.path.exists("./ComfyUI/custom_nodes/ComfyUI-Manager"):
                os.chdir("./ComfyUI/custom_nodes/")
                Repo.clone_from(comfy_manager_url, "ComfyUI-Manager")
                os.chdir("../../")
            
            # installing requirements
            app_logger.log(LoggingType.DEBUG, "Checking comfy requirements, please wait...")
            subprocess.run(["pip", "install", "-r", "./ComfyUI/requirements.txt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            # start the comfy server if not already running
            self.start_server()

            # download custom nodes
            res_custom_nodes = self.download_custom_nodes(workflow, extra_node_urls)
            if not res_custom_nodes['status']:
                app_logger.log(LoggingType.ERROR, res_custom_nodes['message'])
                return

            # download models if not already present
            res_models = self.download_models(workflow, extra_models_list)
            if not res_models['status']:
                app_logger.log(LoggingType.ERROR, res_models['message'])
                if len(res_models['data']['models_not_found']):
                    app_logger.log(LoggingType.INFO, "Please provide custom model urls for the models listed below or modify the workflow json to one of the alternative models listed")
                    for model in res_models['data']['models_not_found']:
                        print("Model: ", model['model'])
                        print("Alternatives: ")
                        if len(model['similar_models']):
                            for alternative in model['similar_models']:
                                print(" - ", alternative)
                        else:
                            print(" - None")
                        print("---------------------------")
                return
            
            # restart the server if custom nodes or models are installed
            if res_custom_nodes['data']['nodes_installed'] or res_models['data']['models_downloaded']:
                app_logger.log(LoggingType.INFO, "Restarting the server")
                self.stop_server()
                self.start_server()

            if len(file_path_list):
                clear_directory("./ComfyUI/input")
                for filepath in file_path_list:
                    copy_files(filepath, "./ComfyUI/input/", overwrite=True)

            # checkpoints, lora, default etc..
            comfy_directory = "./ComfyUI/models/"
            comfy_model_folders = [folder for folder in os.listdir(comfy_directory) if os.path.isdir(os.path.join(comfy_directory, folder))]
            # update model paths e.g. 'v3_sd15_sparsectrl_rgb.ckpt' --> 'SD1.5/animatediff/v3_sd15_sparsectrl_rgb.ckpt'
            for node in workflow:
                if "inputs" in workflow[node]:
                    for key, input in workflow[node]["inputs"].items():
                        if isinstance(input, str) and any(
                            input.endswith(ft) for ft in MODEL_FILETYPES
                        ) and not any(input.endswith(m) for m in OPTIONAL_MODELS):
                            model_path = find_file_in_directory(comfy_directory, input)
                            if model_path:
                                model_path = model_path.replace(comfy_directory, "")
                                if any(model_path.startswith(folder) for folder in comfy_model_folders):
                                    model_path = model_path.split('/', 1)[-1]
                                app_logger.log(LoggingType.DEBUG, f"Updating {input} to {model_path}")
                                workflow[node]["inputs"][key] = model_path

            # get the result
            app_logger.log(LoggingType.INFO, "Generating output please wait")
            client_id = str(uuid.uuid4())
            ws = websocket.WebSocket()
            host = SERVER_ADDR + ":" + str(APP_PORT)
            host = host.replace("http://", "").replace("https://", "")
            ws.connect("ws://{}/ws?clientId={}".format(host, client_id))
            node_output = self.get_output(ws, workflow, client_id, output_node_ids)
            output_list = copy_files("./ComfyUI/output", output_folder, overwrite=False, delete_original=True)
            clear_directory("./ComfyUI/output")

            output_list = {
                'file_paths': output_list,
                'text_output': node_output['text_output']
            }
        except Exception as e:
            app_logger.log(LoggingType.INFO, "Error generating output " + str(e))
        
        # stopping the server
        if stop_server_after_completion:
            self.stop_server()

        # TODO: implement a proper way to remove the logs
        if clear_comfy_logs:
            self.clear_comfy_logs()

        return output_list