import os

import requests
import json
from tqdm import tqdm
from constants import COMFY_MODEL_LIST_PATH

from utils.common import fuzzy_text_match, get_file_size
from utils.logger import LoggingType, app_logger


class FileDownloader:
    def __init__(self):
        pass

    def is_file_downloaded(self, filename, url, dest):
        dest_path = f"{dest}/{filename}"
        app_logger.log(LoggingType.DEBUG, "checking file: ", dest_path)
        if os.path.exists(dest_path):
            return os.path.getsize(dest_path) == get_file_size(url)
        return False

    def download_file(self, filename, url, dest):
        os.makedirs(dest, exist_ok=True)

        # checking if the file is already downloaded
        if self.is_file_downloaded(filename, url, dest):
            app_logger.log(LoggingType.DEBUG, f"{filename} already present")
            return
        else:
            # deleting partial downloads
            if os.path.exists(f"{dest}/{filename}"):
                os.remove(f"{dest}/{filename}")

        # download progress bar
        response = requests.get(url, stream=True)
        total_size = int(response.headers.get('content-length', 0))
        progress_bar = tqdm(total=total_size, unit='B', unit_scale=True)
        with open(f"{dest}/{filename}", "wb") as handle:
            for data in tqdm(response.iter_content(chunk_size=1024)):
                handle.write(data)
                progress_bar.update(len(data))

class ModelDownloader(FileDownloader):
    def __init__(self, model_weights_file_path_list, download_similar_model=False):
        super().__init__()
        self.model_download_dict = self.comfy_model_dict = {}
        self.download_similar_model = download_similar_model

        # loading local data
        for model_weights_file_path in model_weights_file_path_list:
            with open(model_weights_file_path, 'r') as file:
                data = json.load(file)
                for model_name in data:
                    # weight files with lower index have preference
                    if model_name not in self.model_download_dict:
                        self.model_download_dict[model_name] = {
                            'url': data[model_name]['url'],
                            'dest': data[model_name]['dest']
                        }

    def _get_similar_models(self, model_name):
        app_logger.log(LoggingType.DEBUG, "matching model: ", model_name)
        # matching with local data
        model_list = self.model_download_dict.keys()
        similar_models = fuzzy_text_match(model_list, model_name)

        # matching with comfy data
        model_list = self.comfy_model_dict.keys()
        similar_models += fuzzy_text_match(model_list, model_name)

        return similar_models

    def load_comfy_models(self):
        with open(COMFY_MODEL_LIST_PATH, 'rb') as file:
            model_list = json.load(file)["models"]
            
        # loading comfy data
        self.comfy_model_dict = {}
        for model in model_list:
            if model['filename'] not in self.comfy_model_dict:
                self.comfy_model_dict[model['filename']] = [model]
            else:
                self.comfy_model_dict[model['filename']].append(model)

    def download_model(self, model_name):
        # handling nomenclature like "SD1.5/pytorch_model.bin"
        base, model_name = model_name.split("/") if "/" in model_name else ("", model_name)

        if model_name in self.comfy_model_dict:
            for model in self.comfy_model_dict[model_name]:
                if ((base and model['base'] == base) or not base):
                    self.download_file(model['filename'], model['url'], "ComfyUI/models/" + model['save_path'])

        elif model_name in self.model_download_dict:
            self.download_file(
                filename=model_name,
                url=self.model_download_dict[model_name]['url'],
                dest=self.model_download_dict[model_name]['dest']
            )
            
        else:
            app_logger.log(LoggingType.DEBUG, f"Model {model_name} not found in model weights")
            similar_models = self._get_similar_models(model_name)
            if self.download_similar_model and len(similar_models):
                pass
            else:
                return (False, similar_models)
            
        return (True, [])
    