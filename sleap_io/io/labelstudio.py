"""This module handles direct I/O operations for working with .slp files.

Some important nomenclature:
  - `tasks`: typically maps to a single frame of data to be annotated, closest correspondance is to `LabeledFrame`
  - `annotations`: collection of points, polygons, relations, etc. corresponds to `Instance`s and `Point`s, but a flattened hierarchy

"""


import datetime
import json
import math
import uuid
from typing import Dict, Iterable, List, Tuple

from sleap_io import Instance, LabeledFrame, Labels, Node, Point, Video, Skeleton


def read_labels(labels_path: str, skeleton: Skeleton) -> Labels:
    """Read label-studio style annotations from a file and return a `Labels` object.

    Args:
        labels_path: Path to the label-studio annotation file, in json format.
        skeleton: Skeleton

    Returns:
        Parsed labels as a `Labels` instance.
    """
    with open(labels_path, "r") as task_file:
        tasks = json.load(task_file)

    return parse_tasks(tasks, skeleton)


def parse_tasks(tasks: List[Dict], skeleton: Skeleton) -> Labels:
    """Read label-studio style annotations from a file and return a `Labels` object

    Args:
        tasks: collection of tasks to be concerted to `Labels`
        skeleton: Skeleton

    Returns:
        Parsed labels as a `Labels` instance.
    """
    frames: List[LabeledFrame] = []
    for entry in tasks:
        # depending version, we have seen keys `annotations` and `completions`
        if "annotations" in entry:
            key = "annotations"
        elif "completions" in entry:
            key = "completions"
        else:
            raise ValueError("Cannot find annotation data for entry!")

        frames.append(task_to_labeled_frame(entry, skeleton, key=key))

    return Labels(frames)


def write_labels(labels: Labels) -> List[dict]:
    """Convert a `Labels` object into label-studio annotations

    Args:
        labels: Labels to be converted to label-studio task format

    Returns:
        label-studio version of `Labels`
    """

    out = []
    for frame in labels.labeled_frames:
        if frame.video.shape is not None:
            height = frame.video.shape[1]
            width = frame.video.shape[2]
        else:
            height = 100
            width = 100

        frame_annots = []

        for instance in frame.instances:
            inst_id = uuid.uuid4()
            frame_annots.append(
                {
                    "original_width": width,
                    "original_height": height,
                    "image_rotation": 0,
                    "value": {
                        "x": 0,
                        "y": 0,
                        "width": width,
                        "height": height,
                        "rotation": 0,
                        "rectanglelabels": [
                            "instance_class"
                        ],  # TODO: need to handle instance classes / identity
                    },
                    "id": inst_id,
                    "from_name": "individuals",
                    "to_name": "image",
                    "type": "rectanglelabels",
                }
            )

            for node, point in instance.points.items():
                point_id = uuid.uuid4()

                # add this point
                frame_annots.append(
                    {
                        "original_width": width,
                        "original_height": height,
                        "image_rotation": 0,
                        "value": {
                            "x": point.x / width * 100,
                            "y": point.y / height * 100,
                            "keypointlabels": [node.name],
                        },
                        "from_name": "keypoint-label",
                        "to_name": "image",
                        "type": "keypointlabels",
                        "id": point_id,
                    }
                )

                # add relationship of point to individual
                frame_annots.append(
                    {
                        "from_id": point_id,
                        "to_id": inst_id,
                        "type": "relation",
                        "direction": "right",
                    }
                )

        out.append(
            {
                "data": {
                    # 'image': f"/data/{up_deets['file']}"
                },
                "meta": {
                    "video": {
                        "filename": frame.video.filename,
                        "frame_idx": frame.frame_idx,
                        "shape": frame.video.shape,
                    }
                },
                "annotations": [
                    {
                        "result": frame_annots,
                        "was_cancelled": False,
                        "ground_truth": False,
                        "created_at": datetime.datetime.utcnow().strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "updated_at": datetime.datetime.utcnow().strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "lead_time": 0,
                        "result_count": 1,
                        # "completed_by": user['id']
                    }
                ],
            }
        )

    return out


def task_to_labeled_frame(
    task: dict, skeleton: Skeleton, key: str = "annotations"
) -> LabeledFrame:
    """Parse annotations from an entry"""

    if len(task[key]) > 1:
        print(
            "WARNING: Task {}: Multiple annotations found, only taking the first!".format(
                task.get("id", "??")
            )
        )

    try:
        # only parse the first entry result
        to_parse = task[key][0]["result"]

        individuals = filter_and_index(to_parse, "rectanglelabels")
        keypoints = filter_and_index(to_parse, "keypointlabels")
        relations = build_relation_map(to_parse)
        instances = []

        if len(individuals) > 0:
            # multi animal case:
            for indv_id, indv in individuals.items():
                points = {}
                for rel in relations[indv_id]:
                    kpt = keypoints.pop(rel)
                    node = Node(kpt["value"]["keypointlabels"][0])
                    x_pos = (kpt["value"]["x"] * kpt["original_width"]) / 100
                    y_pos = (kpt["value"]["y"] * kpt["original_height"]) / 100

                    # If the value is a NAN, the user did not mark this keypoint
                    if math.isnan(x_pos) or math.isnan(y_pos):
                        continue

                    points[node] = Point(x_pos, y_pos)

                if len(points) > 0:
                    instances.append(Instance(points, skeleton))

        # If this is multi-animal, any leftover keypoints should be unique bodyparts, and will be collected here
        # if single-animal, we only have 'unique bodyparts' [in a way] and the process is identical
        points = {}
        for _, kpt in keypoints.items():
            node = Node(kpt["value"]["keypointlabels"][0])
            points[node] = Point(
                (kpt["value"]["x"] * kpt["original_width"]) / 100,
                (kpt["value"]["y"] * kpt["original_height"]) / 100,
                visible=True,
            )
        if len(points) > 0:
            instances.append(Instance(points, skeleton))

        video, frame_idx = video_from_task(task)

        return LabeledFrame(video, frame_idx, instances)
    except Exception as excpt:
        raise RuntimeError(
            "While working on Task #{}, encountered the following error:".format(
                task.get("id", "??")
            )
        ) from excpt


def filter_and_index(annotations: Iterable[dict], annot_type: str) -> Dict[str, dict]:
    """Filter annotations based on the type field and index them by ID

    Args:
        annotation: annotations to filter and index
        annot_type: annotation type to filter e.x. 'keypointlabels' or 'rectanglelabels'

    Returns:
        Dict[str, dict] - indexed and filtered annotations. Only annotations of type `annot_type`
        will survive, and annotations are indexed by ID
    """
    filtered = list(filter(lambda d: d["type"] == annot_type, annotations))
    indexed = {item["id"]: item for item in filtered}
    return indexed


def build_relation_map(annotations: Iterable[dict]) -> Dict[str, List[str]]:
    """Build a two-way relationship map between annotations

    Args:
        annotations: annotations, presumably, containing relation types

    Returns:
        A two way map of relations indexed by `from_id` and `to_id` fields.
    """
    relations = list(filter(lambda d: d["type"] == "relation", annotations))
    relmap: Dict[str, List[str]] = {}
    for rel in relations:
        if rel["from_id"] not in relmap:
            relmap[rel["from_id"]] = []
        relmap[rel["from_id"]].append(rel["to_id"])

        if rel["to_id"] not in relmap:
            relmap[rel["to_id"]] = []
        relmap[rel["to_id"]].append(rel["from_id"])
    return relmap


def video_from_task(task: dict) -> Tuple[Video, int]:
    """Given a label-studio task, retrieve video information

    Args:
        task: label-studio task

    Returns:
        Video and frame index for this task
    """
    if "meta" in task and "video" in task["meta"]:
        video = Video(task["meta"]["video"]["filename"], task["meta"]["video"]["shape"])
        frame_idx = task["meta"]["video"]["frame_idx"]
        return video, frame_idx

    else:
        raise KeyError("Unable to locate video information for task!", task)
