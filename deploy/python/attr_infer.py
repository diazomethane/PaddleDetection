# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import yaml
import glob
from functools import reduce

import cv2
import numpy as np
import math
import paddle
from paddle.inference import Config
from paddle.inference import create_predictor

import sys
# add deploy path of PadleDetection to sys.path
parent_path = os.path.abspath(os.path.join(__file__, *(['..'])))
sys.path.insert(0, parent_path)

from benchmark_utils import PaddleInferBenchmark
from preprocess import preprocess, Resize, NormalizeImage, Permute, PadStride, LetterBoxResize, WarpAffine
from visualize import visualize_attr
from utils import argsparser, Timer, get_current_memory_mb
from infer import Detector, get_test_images, print_arguments, load_predictor

from PIL import Image, ImageDraw, ImageFont


class AttrDetector(Detector):
    """
    Args:
        pred_config (object): config of model, defined by `Config(model_dir)`
        model_dir (str): root path of model.pdiparams, model.pdmodel and infer_cfg.yml
        device (str): Choose the device you want to run, it can be: CPU/GPU/XPU, default is CPU
        run_mode (str): mode of running(paddle/trt_fp32/trt_fp16)
        batch_size (int): size of pre batch in inference
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to open MKLDNN
        output_dir (str): The path of output
        threshold (float): The threshold of score for visualization
    """

    def __init__(
            self,
            model_dir,
            device='CPU',
            run_mode='paddle',
            batch_size=1,
            trt_min_shape=1,
            trt_max_shape=1280,
            trt_opt_shape=640,
            trt_calib_mode=False,
            cpu_threads=1,
            enable_mkldnn=False,
            output_dir='output',
            threshold=0.5, ):
        super(AttrDetector, self).__init__(
            model_dir=model_dir,
            device=device,
            run_mode=run_mode,
            batch_size=batch_size,
            trt_min_shape=trt_min_shape,
            trt_max_shape=trt_max_shape,
            trt_opt_shape=trt_opt_shape,
            trt_calib_mode=trt_calib_mode,
            cpu_threads=cpu_threads,
            enable_mkldnn=enable_mkldnn,
            output_dir=output_dir,
            threshold=threshold, )

    def get_label(self):
        return self.pred_config.labels

    def postprocess(self, inputs, result):
        # postprocess output of predictor
        im_results = result['output']
        im_results = np.where(im_results < self.threshold, 0, im_results)
        label_list = [['Head', ['Hat', 'Glasses']], [
            'Upper', [
                'ShortSleeve', 'LongSleeve', 'UpperStride', 'UpperLogo',
                'UpperPlaid', 'UpperSplice'
            ]
        ], [
            'Lower', [
                'LowerStripe', 'LowerPattern', 'LongCoat', 'Trousers', 'Shorts',
                'Skirt&Dress'
            ]
        ], ['Shoes', ['boots']], [
            'Accessory',
            ['HandBag', 'ShoulderBag', 'Backpack', 'HoldObjectsInFront']
        ], ['Age', ['AgeOver60', 'Age18-60', 'AgeLess18']],
                      ['Gender', ['Female']],
                      ['Direction', ['Front', 'Side', 'Back']]]

        attr_type = [name[0] for name in label_list]
        labels = self.pred_config.labels

        batch_res = []
        for res in im_results:
            label_res = {}
            label_res = {t: [] for t in attr_type}
            num = 0
            for i in range(len(label_list)):
                type_name_i = attr_type[i]
                attr_name_list = label_list[i][1]
                for attr_name in attr_name_list:
                    attr_name = labels[num]
                    output_prob = res[num]
                    if output_prob != 0:
                        label_res[type_name_i].append(attr_name)
                    num += 1

            if len(label_res['Shoes']) == 0:
                label_res['Shoes'] = ['no boots']
            if len(label_res['Gender']) == 0:
                label_res['Gender'] = ['Male']
            label_res['Age'] = [labels[19 + np.argmax(res[19:22])]]
            label_res['Direction'] = [labels[23 + np.argmax(res[23:])]]
            batch_res.append(label_res)
        result = {'output': batch_res}
        return result

    def predict(self, repeats=1):
        '''
        Args:
            repeats (int): repeats number for prediction
        Returns:
            result (dict): include 'boxes': np.ndarray: shape:[N,6], N: number of box,
                            matix element:[class, score, x_min, y_min, x_max, y_max]
                            MaskRCNN's result include 'masks': np.ndarray:
                            shape: [N, im_h, im_w]
        '''
        # model prediction
        for i in range(repeats):
            self.predictor.run()
            output_names = self.predictor.get_output_names()
            output_tensor = self.predictor.get_output_handle(output_names[0])
            np_output = output_tensor.copy_to_cpu()
        result = dict(output=np_output)
        return result

    def predict_image(self,
                      image_list,
                      run_benchmark=False,
                      repeats=1,
                      visual=True):
        batch_loop_cnt = math.ceil(float(len(image_list)) / self.batch_size)
        results = []
        for i in range(batch_loop_cnt):
            start_index = i * self.batch_size
            end_index = min((i + 1) * self.batch_size, len(image_list))
            batch_image_list = image_list[start_index:end_index]
            if run_benchmark:
                # preprocess
                inputs = self.preprocess(batch_image_list)  # warmup
                self.det_times.preprocess_time_s.start()
                inputs = self.preprocess(batch_image_list)
                self.det_times.preprocess_time_s.end()

                # model prediction
                result = self.predict(repeats=repeats)  # warmup
                self.det_times.inference_time_s.start()
                result = self.predict(repeats=repeats)
                self.det_times.inference_time_s.end(repeats=repeats)

                # postprocess
                result_warmup = self.postprocess(inputs, result)  # warmup
                self.det_times.postprocess_time_s.start()
                result = self.postprocess(inputs, result)
                self.det_times.postprocess_time_s.end()
                self.det_times.img_num += len(batch_image_list)

                cm, gm, gu = get_current_memory_mb()
                self.cpu_mem += cm
                self.gpu_mem += gm
                self.gpu_util += gu
            else:
                # preprocess
                self.det_times.preprocess_time_s.start()
                inputs = self.preprocess(batch_image_list)
                self.det_times.preprocess_time_s.end()

                # model prediction
                self.det_times.inference_time_s.start()
                result = self.predict()
                self.det_times.inference_time_s.end()

                # postprocess
                self.det_times.postprocess_time_s.start()
                result = self.postprocess(inputs, result)
                self.det_times.postprocess_time_s.end()
                self.det_times.img_num += len(batch_image_list)

                if visual:
                    visualize(
                        batch_image_list, result, output_dir=self.output_dir)

            results.append(result)
            if visual:
                print('Test iter {}'.format(i))

        results = self.merge_batch_result(results)
        return results

    def merge_batch_result(self, batch_result):
        if len(batch_result) == 1:
            return batch_result[0]
        res_key = batch_result[0].keys()
        results = {k: [] for k in res_key}
        for res in batch_result:
            for k, v in res.items():
                results[k].extend(v)
        return results


def visualize(image_list, batch_res, output_dir='output'):

    # visualize the predict result
    batch_res = batch_res['output']
    for image_file, res in zip(image_list, batch_res):
        im = visualize_attr(image_file, [res])
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        img_name = os.path.split(image_file)[-1]
        out_path = os.path.join(output_dir, img_name)
        im.save(out_path, quality=95)
        print("save result to: " + out_path)


def main():
    detector = AttrDetector(
        FLAGS.model_dir,
        device=FLAGS.device,
        run_mode=FLAGS.run_mode,
        batch_size=FLAGS.batch_size,
        trt_min_shape=FLAGS.trt_min_shape,
        trt_max_shape=FLAGS.trt_max_shape,
        trt_opt_shape=FLAGS.trt_opt_shape,
        trt_calib_mode=FLAGS.trt_calib_mode,
        cpu_threads=FLAGS.cpu_threads,
        enable_mkldnn=FLAGS.enable_mkldnn,
        threshold=FLAGS.threshold,
        output_dir=FLAGS.output_dir)

    # predict from image
    if FLAGS.image_dir is None and FLAGS.image_file is not None:
        assert FLAGS.batch_size == 1, "batch_size should be 1, when image_file is not None"
    img_list = get_test_images(FLAGS.image_dir, FLAGS.image_file)
    detector.predict_image(img_list, FLAGS.run_benchmark, repeats=10)
    if not FLAGS.run_benchmark:
        detector.det_times.info(average=True)
    else:
        mems = {
            'cpu_rss_mb': detector.cpu_mem / len(img_list),
            'gpu_rss_mb': detector.gpu_mem / len(img_list),
            'gpu_util': detector.gpu_util * 100 / len(img_list)
        }

        perf_info = detector.det_times.report(average=True)
        model_dir = FLAGS.model_dir
        mode = FLAGS.run_mode
        model_info = {
            'model_name': model_dir.strip('/').split('/')[-1],
            'precision': mode.split('_')[-1]
        }
        data_info = {
            'batch_size': FLAGS.batch_size,
            'shape': "dynamic_shape",
            'data_num': perf_info['img_num']
        }
        det_log = PaddleInferBenchmark(detector.config, model_info, data_info,
                                       perf_info, mems)
        det_log('Attr')


if __name__ == '__main__':
    paddle.enable_static()
    parser = argsparser()
    FLAGS = parser.parse_args()
    print_arguments(FLAGS)
    FLAGS.device = FLAGS.device.upper()
    assert FLAGS.device in ['CPU', 'GPU', 'XPU'
                            ], "device should be CPU, GPU or XPU"
    assert not FLAGS.use_gpu, "use_gpu has been deprecated, please use --device"

    main()
