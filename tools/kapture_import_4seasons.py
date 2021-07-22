#!/usr/bin/env python3
# Copyright 2020-present NAVER Corp. Under BSD 3-clause license

"""
Script to import a 4seasons model into a kapture.
The data structure is defined here:
    https://www.4seasons-dataset.com/documentation

The 4Seasons dataset contains recordings from a stereo-inertial camera system coupled with a high-end RTK-GNSS.

For each sequence, the recorded data is stored in the following structure:
├── KeyFrameData
├── distorted_images ...
├── undistorted_images
│   ├── cam0
│   └── cam1
├── GNSSPoses.txt
├── Transformations.txt
├── imu.txt
├── result.txt
├── septentrio.nmea
└── times.txt

The calibration folder has the following structure:
├── calib_0.txt
├── calib_1.txt
├── calib_stereo.txt
├── camchain.yaml
├── undistorted_calib_0.txt
├── undistorted_calib_1.txt
└── undistorted_calib_stereo.txt
"""

import argparse
import logging
import os
import os.path as path
import re
import math
import numpy as np
import quaternion
from glob import glob
from PIL import Image
from tqdm import tqdm
from dataclasses import dataclass
from typing import List, Dict, Tuple, Union
# kapture
import path_to_kapture  # noqa: F401
from kapture.core.Sensors import SENSOR_TYPE_DEPTH_CAM
import kapture
import kapture.utils.logging
from kapture.utils.paths import path_secure
from kapture.io.structure import delete_existing_kapture_files
from kapture.io.csv import kapture_to_dir
import kapture.io.features
from kapture.io.records import TransferAction, import_record_data_from_dir_auto
from kapture.algo.merge_remap import merge_sensors
from kapture.converter.nmea.import_nmea import extract_gnss_from_nmea

logger = logging.getLogger('4seasons')

MASTER_CAM_ID = 'cam0'
RIG_ID = 'car'

q = quaternion.from_rotation_matrix(np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]]))
CAM_AXES_KAPTURE_FROM_4SEASONS = kapture.PoseTransform(r=q)
CAM_AXES_4SEASONS_FROM_KAPTURE = CAM_AXES_KAPTURE_FROM_4SEASONS.inverse()


def load_4seasons_sensors(
        calibration_dir_path: str
) -> (kapture.Sensors, kapture.Rigs):
    # this dataset is made with a single stereo camera (2 cams).

    sensors = kapture.Sensors()
    """
    Pinhole 501.4757919305817 501.4757919305817 421.7953735163109 167.65799492501083 0.0 0.0 0.0 0.0
    800 400
    crop
    800 400
    """
    intrinsic_file_names = {
        'cam0': 'undistorted_calib_0.txt',
        'cam1': 'undistorted_calib_1.txt',
    }
    cam_names = {
        'cam0': 'cam_left',
        'cam1': 'cam_right'
    }
    for cam_id, intrinsic_file_name in intrinsic_file_names.items():
        intrinsic_file_path = path.join(calibration_dir_path, intrinsic_file_name)
        with open(intrinsic_file_path, 'r') as intrinsic_file:
            line = intrinsic_file.readline().split(' ')
            # 1st line looks like:
            #     Pinhole 501.4757919305817 501.4757919305817 421.7953735163109 167.65799492501083 0.0 0.0 0.0 0.0
            # assuming fx fy cx cy distortion coef (null)
            if line[0] != 'Pinhole':  # sanity check
                raise ValueError(f'unexpected camera model {line[0]} (only is Pinhole valid).')
            fx, fy, cx, cy = (float(e) for e in line[1:5])

            line = intrinsic_file.readline().split(' ')
            # second line looks like :
            #   800 400
            # assuming image_width image_height
            w, h = (int(e) for e in line)
            sensors[cam_id] = kapture.Camera(kapture.CameraType.PINHOLE, [w, h, fx, fy, cx, cy], name=cam_names[cam_id])

    # rigs
    rigs = kapture.Rigs()
    stereo_matrix_file_name = path.join(calibration_dir_path, "undistorted_calib_stereo.txt")
    # contains the 4x4 matrix denoting the rigid transformation from the right to the left camera.
    # cam0 = left, cam1 = right
    car_from_cam1_matrix = np.loadtxt(stereo_matrix_file_name)
    r = car_from_cam1_matrix[0:3, 0:3]
    q = quaternion.from_rotation_matrix(r)
    t = car_from_cam1_matrix[0:3, 3]
    cam0_4s_from_cam1_4s = kapture.PoseTransform(q, t)
    cam0_from_cam1 = kapture.PoseTransform.compose([CAM_AXES_KAPTURE_FROM_4SEASONS,
                                                    cam0_4s_from_cam1_4s,
                                                    CAM_AXES_4SEASONS_FROM_KAPTURE])
    rigs[RIG_ID, MASTER_CAM_ID] = kapture.PoseTransform()
    rigs[RIG_ID, 'cam1'] = cam0_from_cam1.inverse()

    # trajectories from
    return sensors, rigs


def load_times_ids(
        times_file_path: str
):
    table = np.loadtxt(times_file_path)
    shots_ids = table[:, 0].astype(int)
    timestamps_ns = (table[:, 1] * 1e9).astype(int)
    shot_id_to_timestamp = {}
    for shot_id, timestamp_ns in zip(shots_ids, timestamps_ns):
        timestamp_ns = int(timestamp_ns)
        shot_id = f'{shot_id:19d}'
        # shot_id should match timestamp in ns
        assert str(timestamp_ns) == shot_id
        shot_id_to_timestamp[shot_id] = timestamp_ns

    return shot_id_to_timestamp


def guess_timestamp(
        shot_id: Union[str, int]
) -> int:
    # improvise a timestamp for shots not registered in times.txt
    # 1602074877621449728 -> 1602074877.621449728 sec -> 1602074877621449.728 u sec
    if not isinstance(shot_id, int):
        shot_id = int(shot_id)
    timestamp = int(float(shot_id) * 1e-9 * 1e6)
    return timestamp


def import_4seasons_images(
        recording_dir_path: str,
        kapture_dir_path: str,
        shot_id_to_timestamp: Dict[str, int],
        sensors: kapture.Sensors,
        images_import_method: TransferAction,
) -> kapture.RecordsCamera:
    kapture_images = kapture.RecordsCamera()
    logger.info('importing images ...')
    season_image_dir_path = path.join(recording_dir_path, 'undistorted_images')
    for sensor_id in sensors:
        for shot_id, timestamp_ns in shot_id_to_timestamp.items():
            image_file_name = path.join(sensor_id, f'{shot_id}.png')
            kapture_images[timestamp_ns, sensor_id] = image_file_name

    filename_list = [f for _, _, f in kapture.flatten(kapture_images)]
    import_record_data_from_dir_auto(
        source_record_dirpath=season_image_dir_path,
        destination_kapture_dirpath=kapture_dir_path,
        filename_list=filename_list,
        copy_strategy=images_import_method)
    return kapture_images


KEYFRAME_TAG_POSE = '# camToWorld:'
KEYFRAME_TAG_CAM = '# fx, fy, cx, cy, width, height'
KEYFRAME_TAG_DEPTH = '# color information'


def read_until_tag(file, tag):
    while True:
        line = file.readline()
        if line == '':
            raise ValueError(f'{tag} not found in file.')
        if line.startswith(tag):
            return line


def load_4season_pose_from_keyframe_data(
        keyframes_file_path: str
):
    # read pose
    with open(keyframes_file_path, 'rt') as file:
        read_until_tag(file, KEYFRAME_TAG_POSE)
        # translation vector, rotation quaternion
        line = file.readline()
    line = [float(v) for v in line.split(',')]
    tx, ty, tz, qx, qy, qz, qw = line  # NOTE: qw is at the end
    car_from_world = kapture.PoseTransform(r=[qw, qx, qy, qz], t=[tx, ty, tz]).inverse()
    # pose found
    return car_from_world


@dataclass
class DepthData:
    f: Tuple[float, float]
    c: Tuple[float, float]
    image_size: Tuple[int, int]
    coords: np.ndarray


def load_4season_depth_from_keyframe_data(
        keyframes_file_path: str
) -> DepthData:
    # read pose
    with open(keyframes_file_path, 'rt') as file:
        read_until_tag(file, KEYFRAME_TAG_CAM)
        fx, fy, cx, cy, width, height, *_ = (float(v) for v in file.readline().split(','))

        read_until_tag(file, KEYFRAME_TAG_DEPTH)
        # read depth and colors
        depth_coords = []
        while True:
            line1 = file.readline().strip()  # u,v,idepth_scaled,...
            _ = file.readline().strip()  # color
            if line1 == '':
                break
            line1 = [float(v) for v in line1.split(',')]
            depth_coords.append(line1[0:3])  # u, v, d

        depth_coords = np.array(depth_coords)
        depth_data = DepthData(f=(fx, fy), c=(cx, cy),
                               image_size=(int(width), int(height)),
                               coords=depth_coords)
        return depth_data


def convert_depth_data_to_depth_map(
        depth_data: DepthData,
        undefined_value=-1.0
) -> np.ndarray:
    """ converts depth map from 4 season format to kapture format """
    depth_map = np.ones(depth_data.image_size, dtype=np.float32)
    depth_map *= undefined_value
    assert depth_data.coords.shape[1] == 3  # u, v, d
    for u, v, d in depth_data.coords:
        # convert d to metric according to given formula
        # d =  inverse depth value in 1/m
        depth_map[int(u), int(v)] = 1. / d if not math.isclose(d, 0.) else 0.
    return depth_map


def import_4seasons_keyframes(
        keyframes_dir_path: str,
        kapture_dir_path: str,
        shot_id_to_timestamp: Dict[str, int],
        sensors: kapture.Sensors,
) -> (kapture.Trajectories, kapture.RecordsDepth):
    """
    imports both trajectories and depth maps

    :param keyframes_dir_path:
    :param kapture_dir_path:
    :param shot_id_to_timestamp:
    :param sensors:
    :return:
    """
    trajectories = kapture.Trajectories()
    depth_maps = kapture.RecordsDepth()

    # make sure depth maps hosting directory exists
    depth_records_path = kapture.io.records.get_depth_map_fullpath(kapture_dir_path)
    os.makedirs(depth_records_path, exist_ok=True)
    #                                   KeyFrame_1602074967051661568.txt
    keyframe_filename_re = re.compile(r'^KeyFrame_(?P<shot_id>\d{19})\.txt$')
    filename_it = (path.basename(f) for f in glob(path.join(keyframes_dir_path, '*.txt')))
    filename_it = (filename for filename in filename_it
                   if keyframe_filename_re.match(filename))
    filename_it = (keyframe_filename_re.match(filename) for filename in filename_it)
    filename_it = {filename[0]: filename[1] for filename in filename_it}
    for filename, shot_id in filename_it.items():
        assert shot_id in shot_id_to_timestamp
        timestamp_ns = shot_id_to_timestamp[shot_id]
        keyframes_file_path = path.join(keyframes_dir_path, filename)

        # kapture.io.records.get_depth_map_fullpath()
        car_from_world = load_4season_pose_from_keyframe_data(keyframes_file_path)
        trajectories[timestamp_ns, 'car'] = car_from_world

        season_depth_data = load_4season_depth_from_keyframe_data(keyframes_file_path)
        # records_depth_to_file
        depth_map_file_name = f'{MASTER_CAM_ID}/{shot_id}.depth'
        depth_maps[timestamp_ns, MASTER_CAM_ID] = depth_map_file_name
        depth_map = convert_depth_data_to_depth_map(season_depth_data)
        depth_map_file_path = kapture.io.records.get_depth_map_fullpath(kapture_dir_path, depth_map_file_name)
        kapture.io.records.records_depth_to_file(depth_map_file_path, depth_map)

    return trajectories, depth_maps


def import_4seasons_imu(
        imu_file_path: str,
        shot_id_to_timestamp: Dict[str, int]
) -> (kapture.Sensors, kapture.RecordsAccelerometer, kapture.RecordsGyroscope):
    sensors = kapture.Sensors()
    accelerometer_id, gyroscope_id = 'accelero', 'gyro'
    sensors[accelerometer_id] = kapture.Sensor(
        sensor_type='accelerometer', name=accelerometer_id)
    sensors[gyroscope_id] = kapture.Sensor(
        sensor_type='gyroscope', name=gyroscope_id)

    accelerometer = kapture.RecordsAccelerometer()
    gyroscope = kapture.RecordsGyroscope()
    # Each line is specified as frame_id, (angular velocity (w_x, w_y, w_z), and linear acceleration (a_x, a_y, a_z)).
    # 1602074877342319360 -0.009163 0.018326 -0.070250 0.189211 0.860048 9.657110
    data = np.loadtxt(imu_file_path)
    shot_ids = data[:, 0].astype(np.int).astype(str)
    rotation_speeds = data[:, 1:4]
    translation_accels = data[:, 4:7]
    for shot_id, (rx, ry, rz), (ax, ay, az) in zip(shot_ids, rotation_speeds, translation_accels):
        timestamp = shot_id_to_timestamp.get(shot_id, guess_timestamp(shot_id))
        accelerometer[timestamp, accelerometer_id] = kapture.RecordAccelerometer(ax, ay, az)
        gyroscope[timestamp, gyroscope_id] = kapture.RecordGyroscope(rx, ry, rz)
    return sensors, accelerometer, gyroscope


def import_4seasons_sequence(
        calibration_dir_path: str,
        recording_dir_path: str,
        kapture_dir_path: str,
        images_import_method: TransferAction,
        force_overwrite_existing: bool):
    """
    converts a 4 seasons recorded sequence (eg. recording_2020-10-07_14-47-51) to kapture format.

    :param calibration_dir_path: path to input calibration directory
    :param recording_dir_path: path to input sequence directory (e.g. recording_2020-10-07_14-47-51)
    :param kapture_dir_path: path to output kapture directory
    :param images_import_method:
    :param force_overwrite_existing:
    :return:
    """
    delete_existing_kapture_files(kapture_dir_path, force_erase=force_overwrite_existing)
    os.makedirs(kapture_dir_path, exist_ok=True)

    """
    recording_dir_path contains : 
    KeyFrameData: contains the KeyFrameFiles.
    distorted_images: contains both the distorted images from the left and right camera, respectively.
    undistorted_images: contains both the undistorted images from the left and right camera, respectively.
    GNSSPoses.txt: is a list of 7DOF globally optimized poses (include scale from VIO to GNSS frame) for all keyframes (after GNSS fusion and loop closure detection). Each line is specified as frame_id, translation (t_x, t_y, t_z), rotation as quaternion (q_x, q_y, q_z, w), scale, fusion_quality (not relevant), and v3 (not relevant).
    Transformations.txt: defines transformations between different coordinate frames.
    imu.txt: contains raw IMU measurements. Each line is specified as frame_id, (angular velocity (w_x, w_y, w_z), and linear acceleration (a_x, a_y, a_z)).
    result.txt: contains the 6DOF visual interial odometry poses for every frame (not optimized). Each line is specified as timestamp (in seconds), translation (t_x, t_y, t_z), rotation as quaternion (q_x, q_y, q_z, w).
    septentrio.nmea: contains the raw GNSS measurements in the NMEA format.
    times.txt is a list of times in unix timestamps (in seconds), and exposure times (in milliseconds) for each frame (frame_id, timestamp, exposure).
    """

    # sensors
    sensors, rigs = load_4seasons_sensors(calibration_dir_path=calibration_dir_path)
    imported_kapture = kapture.Kapture(sensors=sensors, rigs=rigs)

    # timestamps
    times_filename = path.join(recording_dir_path, 'times.txt')
    shot_id_to_timestamp = load_times_ids(times_filename)

    # images
    records_camera = import_4seasons_images(
        recording_dir_path=recording_dir_path,
        kapture_dir_path=kapture_dir_path,
        shot_id_to_timestamp=shot_id_to_timestamp,
        sensors=sensors,
        images_import_method=images_import_method
    )
    imported_kapture.records_camera = records_camera

    # trajectories uses keyframes
    keyframes_dir_path = path.join(recording_dir_path, 'KeyFrameData')
    trajectories, depth_maps = import_4seasons_keyframes(
        keyframes_dir_path=keyframes_dir_path,
        kapture_dir_path=kapture_dir_path,
        shot_id_to_timestamp=shot_id_to_timestamp,
        sensors=sensors,
    )
    imported_kapture.trajectories = trajectories

    # imu.txt to accel and gyro
    imu_file_path = path.join(recording_dir_path, 'imu.txt')
    imu_sensors, records_accelerometer, records_gyroscope = import_4seasons_imu(
        imu_file_path=imu_file_path,
        shot_id_to_timestamp=shot_id_to_timestamp)
    sensors.update(imu_sensors)
    imported_kapture.records_accelerometer = records_accelerometer
    imported_kapture.records_gyroscope = records_gyroscope
    # TODO: register the imu into the rig.

    # GNSS data
    nmea_file_path = path.join(recording_dir_path, 'septentrio.nmea')
    gnss_sensors, records_gnss = extract_gnss_from_nmea(
        nmea_file_path=nmea_file_path, gnss_id='GNSS'
    )
    sensors.update(gnss_sensors)
    imported_kapture.records_gnss = records_gnss
    gnss_id = next(iter(gnss_sensors))
    imported_kapture.rigs[RIG_ID, gnss_id] = kapture.PoseTransform()

    # finally save the kapture csv files
    kapture_to_dir(kapture_dir_path, imported_kapture)


def import_4seasons_command_line() -> None:
    """
    Imports 4seasons dataset and save them as kapture using the parameters given on the command line.
    It assumes images are undistorted.
    """
    parser = argparse.ArgumentParser(
        description='Imports 4seasons dataset files to the kapture format.')
    parser_verbosity = parser.add_mutually_exclusive_group()
    parser_verbosity.add_argument(
        '-v', '--verbose', nargs='?', default=logging.WARNING, const=logging.INFO,
        action=kapture.utils.logging.VerbosityParser,
        help='verbosity level (debug, info, warning, critical, ... or int value) [warning]')
    parser_verbosity.add_argument(
        '-q', '--silent', '--quiet', action='store_const', dest='verbose', const=logging.CRITICAL)
    parser.add_argument('-f', '-y', '--force', action='store_true', default=False,
                        help='Force delete output if already exists.')
    # import ###########################################################################################################
    parser.add_argument('-i', '--input', required=True,
                        help='input path to 4 seasons record directory (e.g. ./recording_2020-10-07_14-47-51)')
    parser.add_argument('-c', '--calibration', required=False,
                        help='input path to 4 seasons calibration. If not given, assumed to be alongside input.')
    parser.add_argument('--image_transfer', type=TransferAction, default=TransferAction.link_absolute,
                        help=f'How to import images [link_absolute], '
                             f'choose among: {", ".join(a.name for a in TransferAction)}')
    parser.add_argument('-o', '--output', required=True, help='output directory.')
    ####################################################################################################################
    args = parser.parse_args()

    logger.setLevel(args.verbose)
    if args.verbose <= logging.DEBUG:
        # also let kapture express its logs
        kapture.utils.logging.getLogger().setLevel(args.verbose)

    recording_dir_path = path.abspath(args.input)
    calibration_dir_path = args.calibration or path.join(recording_dir_path, '..', 'calibration')
    calibration_dir_path = path.abspath(calibration_dir_path)

    import_4seasons_sequence(
        calibration_dir_path=calibration_dir_path,
        recording_dir_path=recording_dir_path,
        kapture_dir_path=args.output,
        images_import_method=args.image_transfer,
        force_overwrite_existing=args.force)


if __name__ == '__main__':
    import_4seasons_command_line()
