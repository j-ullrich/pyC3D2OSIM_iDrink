# Extract marker locations from C3D files and convert them into OpenSim TRC format

import argparse
from io import TextIOWrapper

import c3d
import csv
from itertools import compress
import numpy as np
import sys
import math
from scipy.spatial.transform import Rotation
import scipy
import scipy.interpolate
import pandas as pd
import xml.etree.ElementTree as ET

# Command-line interface definition
parser = argparse.ArgumentParser(description='Extract marker locations from C3D files and save in TRC format.')
parser.add_argument('--input_file', metavar='I', type=argparse.FileType('rb'), default=None,
                    help='C3D file to use')
parser.add_argument('--output_file', metavar='O', type=argparse.FileType('w'), default=sys.stdout,
                    help='TRC file to write')
parser.add_argument('--markers', metavar='M', type=lambda kv: kv.split('=', 1), nargs='+', action='append',
                    default=None, help='List of markers to read and optionally renamed')
parser.add_argument('--origin_marker', metavar='R', type=str, default=None,
                    help='Transform markers relative to this marker')
parser.add_argument('--axes_markers', metavar='X', type=str, nargs='+', default=[],
                    help='Rotate all markers along the axes defined by these markers eg. xyz 1 x2 y1 y2 z1 z2')
parser.add_argument('--osim_model', metavar='M', type=argparse.FileType('r'),
                    help='OpenSim model to use for transformations')
parser.add_argument('--mocap_transform', metavar='T', type=str, nargs='+', default=[],
                    help='MoCap system name or list of rotations in degrees along the axes in the OpenSim coordinate '
                         'system optionally trailed by axes order for swapping eg. yxz 90 180 0.')
parser.add_argument('--resample', metavar='S', type=int, default=0,
                    help='Resample data using second order spline to desired rate in Hz.')
parser.add_argument('--c3d_folder', metavar='F', type=str, default=None,
                    help='Folder containing C3D files to process')
parser.add_argument('--root_folder', metavar='RF', type=str, default=None,
                    help='Folder containing Subfolders with C3D files to process')
parser.add_argument('--DEBUG', action='store_true', default=False,
                    help='Debug mode')

# Loads marker position from .osim file
def loadOSIM(file):
    xml = ET.parse(file).getroot()
    units = xml.find('*/length_units').text
    multiplier = 1
    if units == 'meters':
        multiplier = 1000
    markers = xml.findall('*/*/*/Marker')
    keys = map(lambda x: x.attrib['name'], markers)
    values = map(lambda x: np.array(x.find('location').text.strip().split(' '), dtype=np.float)*multiplier, markers)
    return dict(zip(keys,values))

# Load TRC file
def loadTRC(file):

    tfile = TextIOWrapper(file)

    # Skip first line and read metadata
    tfile.readline()
    reader = csv.DictReader(tfile, delimiter='\t')
    data = dict(map(lambda kv: kv if kv[0] == 'Units' else (kv[0], int(kv[1])), reader.__next__().items()))

    # Get marker names
    reader = csv.reader(tfile, delimiter='\t')
    data["Labels"] = list(filter(lambda x: x != '', map(lambda x: x.strip(), reader.__next__())))[2:]
    reader.__next__()

    # Read data
    data["Data"] = np.empty(shape=(data["NumMarkers"], data["NumFrames"], 3), dtype=np.float)
    data["Timestamps"] = np.empty(shape=(data["OrigNumFrames"]), dtype=np.float)
    for row in reader:
        data["Timestamps"][int(row[0])] = float(row[1])
        for label in range(len(data["Labels"])):
            data["Data"][label][int(row[0])] = list(map(lambda x: float(x), row[2+label*3:2+(label+1)*3]))

    return data


# Loads a C3D file and maps variables to TRC variables
def loadC3D(file):
    reader = c3d.Reader(file)
    data = dict()
    data["NumFrames"] = reader.header.last_frame-reader.header.first_frame+1
    data["DataRate"] = reader.header.frame_rate
    data["CameraRate"] = reader.header.frame_rate
    data["NumMarkers"] = len(reader.point_labels)
    data["Units"] = "mm"
    data["OrigDataRate"] = reader.header.frame_rate
    data["OrigDataStartFrame"] = reader.header.first_frame
    data["OrigNumFrames"] = reader.header.last_frame-reader.header.first_frame+1
    data["Labels"] = list(map(lambda x: x.strip(), reader.point_labels))
    data["Timestamps"] = np.arange(0, data["NumFrames"]*1/data["DataRate"], 1/data["DataRate"])

    data["Data"] = np.empty(shape=(data["NumMarkers"], data["NumFrames"], 3), dtype=np.float)
    for i, points, analog in reader.read_frames():
        for label, frameData in enumerate(points[:,0:3]):
            data["Data"][label][i-reader.header.first_frame] = frameData

    return data

# Resample data to desired Hz
def resample(data, targetRate):

    numFrames = math.ceil(len(data["Data"][0])/(data["DataRate"]/targetRate))
    sampledData = np.ndarray(shape=(len(data["Data"]), numFrames, 3), dtype=np.float64)
    for i in range(len(data["Data"])):
        sourceX = data["Timestamps"]
        targetX = np.linspace(0, sourceX[-1], num=numFrames)
        interp = scipy.interpolate.interp1d(sourceX, data["Data"][i], kind='nearest', axis=0)
        sampledData[i] = interp(targetX)


    data["Data"] = sampledData
    data["DataRate"] = targetRate
    data["NumFrames"] = numFrames
    data["Timestamps"] = np.arange(0, data["NumFrames"] * 1 / data["DataRate"], 1 / data["DataRate"])
    return data

# Translates marker positions relative to another marker
def translateToOrigin(data, origLabel):
    data["Data"] = data["Data"] - data["Data"][data["Labels"].index(origLabel)]
    return data

# Rotate markers around axes specified by markers
def rotateAroundAxes(data, rotations, modelMarkers):

    if len(rotations) != len(rotations[0]*2) + 1:
        raise ValueError("Correct format is order of axes followed by two marker specifying each axis")

    for a, axis in enumerate(rotations[0]):

        markerName1 = rotations[1+a*2]
        markerName2 = rotations[1 + a*2 + 1]
        marker1 = data["Labels"].index(markerName1)
        marker2 = data["Labels"].index(markerName2)
        axisIdx = ord(axis) - ord('x')
        if (0<=axisIdx<=2) == False:
            raise ValueError("Axes can only be x y or z")

        origAxis = [0,0,0]
        origAxis[axisIdx] = 1
        if modelMarkers is not None:
            origAxis = modelMarkers[markerName1] - modelMarkers[markerName2]
            origAxis /= scipy.linalg.norm(origAxis)
        rotateAxis = data["Data"][marker1] - data["Data"][marker2]
        rotateAxis /= scipy.linalg.norm(rotateAxis, axis=1, keepdims=True)

        for i, rotAxis in enumerate(rotateAxis):
            angle = np.arccos(np.clip(np.dot(origAxis, rotAxis), -1.0, 1.0))
            r = Rotation.from_euler('y', -angle)
            data["Data"][:,i] = r.apply(data["Data"][:,i])


    return data

# Filters unnecessary markers and renames them
def filterMarkers(data, markers):

    # Filter markers
    data["NumMarkers"] = len(markers)
    orig_markers = list(map(lambda x: x[0], markers))
    list_filter = list(map(lambda x: x in orig_markers, data["Labels"]))
    data["Labels"] = list(compress(data["Labels"], list_filter))
    data["Data"] = data["Data"][list_filter]

    # Rename markers
    remap = dict(map(lambda x: (x[0], x[1] if len(x) == 2 else None), markers))
    for i, label in enumerate(data["Labels"]):
        if remap[label] is not None:
            data["Labels"][i] = remap[label]

    return data

# Transform coordinates from lab coordinate system to OpenSim coordinate system
def mocapTransform(data, transform):

    permutation = [0,1,2]
    if len(transform) == 1:
        if transform[0] == 'qualisys':
            permutation = [0, 1, 2]
            rotations = [90, 180, 0]
        else:
            raise ValueError("Unknown mocap system")
    elif len(transform) == 3:
        rotations = transform
    elif len(transform) == 4:
        rotations = transform[1:4]
        for i in range(3):
            permutation[i] = ord(transform[0][i])-ord('x')
            if permutation[i] > 2 or permutation[i] < 0:
                raise ValueError("Incorrect value for axes order")

    else:
        raise ValueError("Transform must be a known mocap system or a list of rotation angles optionally trailed by "
                         "order of axes")

    axes = "".join(map(lambda x: chr(x+ord('X')), permutation))
    rot = Rotation.from_euler(axes, np.array(rotations)[permutation], degrees=True)
    data["Data"] = rot.apply(data["Data"].reshape(-1,3)[:,permutation]).reshape(data["Data"].shape)

    return data

# Writes the data in a TRC file
def writeTRC(data, file):

    # Write header
    file.write("PathFileType\t4\t(X/Y/Z)\toutput.trc\n")
    file.write("DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\tOrigDataRate\tOrigDataStartFrame\tOrigNumFrames\n")
    file.write("%d\t%d\t%d\t%d\tmm\t%d\t%d\t%d\n" % (data["DataRate"], data["CameraRate"], data["NumFrames"],
                                                     data["NumMarkers"], data["OrigDataRate"],
                                                     data["OrigDataStartFrame"], data["OrigNumFrames"]))

    # Write labels
    file.write("Frame#\tTime\t")
    for i, label in enumerate(data["Labels"]):
        if i != 0:
            file.write("\t")
        file.write("\t\t%s" % (label))
    file.write("\n")
    file.write("\t")
    for i in range(len(data["Labels"]*3)):
        file.write("\t%c%d" % (chr(ord('X')+(i%3)), math.ceil((i+1)/3)))
    file.write("\n")

    # Write data
    for i in range(len(data["Data"][0])):
        file.write("%d\t%f" % (i, data["Timestamps"][i]))
        for l in range(len(data["Data"])):
            file.write("\t%f\t%f\t%f" % tuple(data["Data"][l][i]))
        file.write("\n")

def process_c3d_folder(c3d_folder, mocap_transform):
    """
    Iterate through all c3d files in c3d_folder and process them.
    Allows Mocap Transformation. fo each file

    Input:
     - c3d_folder - string
     - mocap_transform - list of strings
    """
    import os

    for subdir, dirs, files in os.walk(root_folder):
        for file in files:
            if file.endswith('.c3d'):
                c3d_file = os.path.join(subdir, file)

                trc_file = c3d_file.replace('.c3d', '.trc')
                trc_folder = os.path.join(os.path.split(os.path.split(trc_file)[0])[0], 'trc')
                if not os.path.exists(trc_folder):
                    os.makedirs(trc_folder)
                trc_file = os.path.join(trc_folder, os.path.split(trc_file)[1])

                with open(c3d_file, 'rb') as c3d_file, open(trc_file, 'w') as trc_file:
                    data = loadC3D(c3d_file)

                    # MoCap transformation
                    if len(mocap_transform) > 0:
                        mocapTransform(data, mocap_transform)

                    writeTRC(data, trc_file)
                    print("Processed: ", trc_file.name)

def process_root_folder(root_folder, DEBUG = False):
    """
    Iterate thorugh all subfolders in root folder.
    Process all c3d Files in each subfolder.

    Input: root_folder - string
    """
    import os

    labellist1 = ["shoulder_R",
                  "shoulder_L",
                  "elbow_R",
                  "arm_R",
                  "arm_L",
                  "middlefinger_L",
                  "head",
                  "hand_L",
                  "wrist_inner_R",
                  "wrist_inner_L",
                  "hand_R",
                  "elbow_L",
                  "middlefinger_R",
                  "indexfinger_R",
                  "wrist_outer_L",
                  "chest",
                  "indexfinger_L",
                  "wrist_outer_R",
                  "hip_R",
                  "thumb_L",
                  "hip_L",
                  "thumb_R"
                  ]

    #This list is used for P01
    labellist2 = ["Rshoulder",
                  "Lshoulder",
                  "Relbow",
                  "Rarm",
                  "Larm",
                  "Lmiddlefinger",
                  "head",
                  "Lhand",
                  "Rwrist_inner",
                  "Lwrist_inner",
                  "Rhand",
                  "Lelbow",
                  "Rmiddlefinger",
                  "Rindexfinger",
                  "Lwrist",
                  "chest",
                  "Lindexfinger",
                  "Rwrist",
                  "hip_R",
                  "Lthumb",
                  "hip_L",
                  "Rthumb"]
    used_labels = None


    for subdir, dirs, files in os.walk(root_folder):
        for file in files:
            if file.endswith('.c3d'):
                c3d_file = os.path.join(subdir, file)

                trc_file = c3d_file.replace('.c3d', '.trc')
                trc_folder = os.path.join(os.path.split(os.path.split(trc_file)[0])[0], 'trc')
                if not os.path.exists(trc_folder):
                    os.makedirs(trc_folder)

                trc_file = os.path.join(trc_folder, os.path.split(trc_file)[1])
                with open(c3d_file, 'rb') as c3d_file, open(trc_file, 'w') as trc_file:
                    data = loadC3D(c3d_file)

                    # Check used nomenclature in data["labels"]
                    if "body_shoulder_R" in data["Labels"]:
                        used_labels = "labellist1"
                    else:
                        used_labels = "labellist2"


                    if DEBUG:
                        # Take all Data of clusters out. and rename the remaining labels
                        # Take all Data of clusters out. and rename the remaining labels
                        for i in range(len(data["Labels"]) - 1, -1, -1):
                            """
                            if the label contains "cluster" then remove it from the labels and the corresponding data 
                            from data["Data"].

                            If the label contains "body_" take "body_" out of the labelname. e.g. "body_head" -> "head"
                            """
                            try:
                                if "body_" in data["Labels"][i]:
                                    if used_labels == "labellist1":
                                        data["Labels"][i] = data["Labels"][i].replace("body_", "")
                                    else:
                                        label = data["Labels"][i].replace("body_", "")
                                        data["Labels"][i] = labellist1[labellist2.index(label)]
                                if "cluster" in data["Labels"][i]:
                                    data["Labels"].remove(data["Labels"][i])
                                    data["Data"] = np.delete(data["Data"], i, 0)
                            except Exception as e:
                                print("Error: ", e)
                                print("Current i: ", i)
                                print("Data_labels: ", data["Labels"])
                                print("Data_len_labels: ", len(data["Labels"]))
                                print("Data_shape: ", data["Data"].shape)
                                print("Data_keys: ", data.keys())
                                sys.exit(0)

                        data["NumMarkers"] = len(data["Labels"])

                        writeTRC(data, trc_file)
                        print("Processed: ", trc_file.name)

                        sys.exit(0)

                    # Take all Data of clusters out. and rename the remaining labels
                    for i in range(len(data["Labels"]) - 1, -1, -1):
                        """
                        if the label contains "cluster" then remove it from the labels and the corresponding data 
                        from data["Data"].

                        If the label contains "body_" take "body_" out of the labelname. e.g. "body_head" -> "head"
                        """
                        try:
                            if "body_" in data["Labels"][i]:
                                if used_labels == labellist1:
                                    data["Labels"][i] = data["Labels"][i].replace("body_", "")
                                else:
                                    label = data["Labels"][i].replace("body_", "")
                                    data["Labels"][i] = labellist1[labellist2.index(label)]
                            if "cluster" in data["Labels"][i]:
                                data["Labels"].remove(data["Labels"][i])
                                data["Data"] = np.delete(data["Data"], i, 0)
                        except Exception as e:
                            print("Error: ", e)
                            print("Current i: ", i)
                            print("Data_labels: ", data["Labels"])
                            print("Data_len_labels: ", len(data["Labels"]))
                            print("Data_shape: ", data["Data"].shape)
                            print("Data_keys: ", data.keys())
                            sys.exit(0)

                    data["NumMarkers"] = len(data["Labels"])

                    mocapTransform(data, ["qualisys"])

                    writeTRC(data, trc_file)
                    print("Processed: ", trc_file.name)


if __name__ == '__main__':
    # Parse command line arguments
    args = parser.parse_args()

    #args.DEBUG = True
    # Debug mode
    if args.DEBUG:
        root_folder = r"C:\iDrink\Data\P01"
        process_root_folder(root_folder, DEBUG=True)

    # Process root_folder
    # command example: python extractMarkers.py --root_folder C:\iDrink\Data
    if args.root_folder is not None:
        process_root_folder(args.root_folder)

    # process c3d_folder
    if args.c3d_folder is not None:
        process_c3d_folder(args.c3d_folder, args.mocap_transform)

    # Load input file
    if args.input_file is not None:
        if args.input_file.name[-4:] == '.trc':
            data = loadTRC(args.input_file)
        else:
            data = loadC3D(args.input_file)

        modelMarkers = None
        if args.osim_model is not None:
            modelMarkers = loadOSIM(args.osim_model)

        # Resample data
        if args.resample > 0:
            resample(data, args.resample)

        # Translate markers relative to origin marker
        if args.origin_marker is not None:
            translateToOrigin(data, args.origin_marker)

        # Delete unnecessary markers and rename them
        if args.markers is not None:
            filterMarkers(data, args.markers[0])

        # MoCap transformation
        if len(args.mocap_transform) > 0:
            mocapTransform(data, args.mocap_transform)

        if len(args.axes_markers) > 0:
            rotateAroundAxes(data, args.axes_markers, modelMarkers)

        # Write the data into the TRC file
        writeTRC(data, args.output_file)

        # Clean up
        args.input_file.close()
        args.output_file.close()