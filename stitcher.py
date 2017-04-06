#!/usr/bin/env python3
# Licensed under the MIT License (c) 2017 Kevin Haroldsen

import math
import logging
from typing import Union, List, Optional, Tuple

import cv2
import numpy as np
import scipy.sparse.csr as csr
import scipy.sparse.csgraph as csgraph
import matplotlib.pyplot as plt


log = logging.getLogger('stitcher')


try:
    feature_finder = cv2.xfeatures2d.SIFT_create()
    matcher = cv2.BFMatcher_create(cv2.NORM_L2)
except AttributeError:
    raise ImportError('You do not have OpenCV SIFT support installed')


def update_defaults(obj, kwargs):
    for k, v in kwargs.items():
        if not hasattr(obj, k):
            raise NameError("Class '%s' does not have an attribute '%s'" % (
                obj.__class__.__name__, k))
        setattr(obj, k, v)


def image_corners(arr):
    return np.array([
        [0., 0.],
        [0., arr.shape[1]],
        arr.shape[:2],
        [arr.shape[0], 0.],
    ])


def fitting_rectangle(*points):
    # Return (left, top), (width, height)
    top = left = float('inf')
    right = bottom = float('-inf')
    for x, y in points:
        if x < left:
            left = x
        if x > right:
            right = x
        if y < top:
            top = y
        if y > bottom:
            bottom = y
    left = int(math.floor(left))
    top = int(math.floor(top))
    width = int(math.ceil(right - left))
    height = int(math.ceil(bottom - top))
    return (left, top), (width, height)


def paste_image(base, img, shift):
    """Fast image paste with transparency support and no bounds-checking"""
    assert base.dtype == np.uint8 and img.dtype == np.uint8
    h, w = img.shape[:2]
    x, y = shift
    dest_slice = np.s_[y:y + h, x:x + w]
    dest = base[dest_slice]
    mask = (255 - img[..., 3])
    assert mask.dtype == np.uint8
    assert mask.shape == dest.shape[:2], (mask.shape, dest.shape[:2])
    dest_bg = cv2.bitwise_and(dest, dest, mask=mask)
    assert dest_bg.dtype == np.uint8
    dest = cv2.add(dest_bg, img)
    base[dest_slice] = dest


def imshow(img, title=None, figsize=None, **kwargs):
    if figsize is None:
        plt.plot()
    else:
        plt.figure(figsize=figsize)
    plt.axis('off')
    if title is not None:
        plt.title(title)
    plt.imshow(img, interpolation='bicubic', **kwargs)
    plt.show()


class _StitchImage:
    _lastIdx = 1

    def __init__(self, image, name: str=None):
        self.image = image
        self.kp = None
        self.feat = None

        if name is None:
            name = '%02d' % (_StitchImage._lastIdx)
            _StitchImage._lastIdx += 1
        self.name = name

    def find_features(self):
        log.debug('Finding features for image %s', self.name)
        self.kp, self.feat = feature_finder.detectAndCompute(self.image, None)


class ImageStitcher:
    def __init__(self, **kwargs):
        self._matches = {}
        self._images = []

        self.ratio_threshold = 0.7
        self.matches_threshold = 10
        self._center = None
        self._current_edge_matrix = None
        self.debug = False

        update_defaults(self, kwargs)

    def add_image(self, image: Union[str, np.ndarray], name: str=None):
        """Add an image to the current stitching process. Image must be RGB(A)"""
        if isinstance(image, str):
            if name is not None:
                name = image
            image = cv2.cvtColor(cv2.imread(image), cv2.COLOR_BGR2RGBA)
        if image.shape[-1] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2RGBA)
        image = _StitchImage(image, name=name)
        image.find_features()
        idx = len(self._images)
        self._images.append(image)

        for oidx, other in enumerate(self._images[:-1]):
            match = self._match_features(image, other)
            if match is not None:
                self._matches[(idx, oidx)] = match

    @property
    def center(self) -> int:
        if self._center is None:
            self._center = self._find_center()
        return self._center

    @center.setter
    def center(self, val: int):
        self._center = val

    def stitch(self):
        """Perform the actual stitching - return the image of the result."""
        self.validate()
        log.info('%s considered center image', self._images[self.center].name)
        parents = csgraph.dijkstra(
            self._edge_matrix,
            directed=False, indices=self.center,
            return_predecessors=True,
        )[1]
        log.debug('Parent matrix:\n%s', parents)
        Hs = self._calculate_total_homographies(parents)
        all_new_corners = self._calculate_new_corners(Hs)
        base_shift, base_size = np.array(self._calculate_bounds(all_new_corners))
        order = self._calculate_draw_order(parents)
        canvas = np.zeros((base_size[1], base_size[0], 4), dtype=np.uint8)
        for i in order:
            image = self._images[i]
            new_corners = all_new_corners[i]
            H = Hs[i]

            shift, size = np.array(fitting_rectangle(*new_corners))
            dest_shift = shift - base_shift
            log.info('Pasting %s @ (%d, %d)', image.name, *dest_shift)

            log.debug('Shifting %s by (%d, %d)', image.name, *shift)
            log.debug('Transformed %s is %dx%d', image.name, *size)
            T = np.array([[1, 0, -shift[0]], [0, 1, -shift[1]], [0, 0, 1]])
            Ht = T.dot(H)
            log.debug('Translated homography:\n%s', Ht)
            new_image = cv2.warpPerspective(
                image.image, Ht, tuple(size),
                flags=cv2.INTER_LINEAR,
            )
            paste_image(canvas, new_image, dest_shift)
        log.info('Done!')
        return canvas

    def validate(self):
        cc, groups = csgraph.connected_components(self._edge_matrix, directed=False)
        if cc != 1:
            most_common = np.bincount(groups).argmax()
            raise ValueError('Image(s) %s could not be stitched' % ','.join(
                self._images[img].name for img in np.where(groups != most_common)[0]
            ))

    def _calculate_new_corners(self, Hs) -> List[np.array]:
        all_new_corners = []
        for image, H in zip(self._images, Hs):
            corners = image_corners(image.image)
            new_corners = cv2.perspectiveTransform(np.array([corners]), H)
            if new_corners.shape[0] != 1:
                raise ValueError('Could not calculate bounds for %s!' % image.name)
            new_corners = new_corners[0]
            log.debug(
                '%s transform: (%s,%s,%s,%s)->(%s,%s,%s,%s)',
                image.name, *(
                    '(%s)' % ','.join(str(int(round(i))) for i in arr)
                    for arr in (*corners, *new_corners)),
            )
            all_new_corners.append(new_corners)
        return all_new_corners

    def _calculate_bounds(self, new_corners) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """Calculate the bounds required to hold all images transformed with the given corners"""
        all_corners = []
        for corners in new_corners:
            all_corners.extend(corners)
        log.debug('%d new corners to calculate bounds with', len(all_corners))
        corner, size = fitting_rectangle(*all_corners)
        log.info('Center at: %r', (-corner[0], -corner[1]))
        log.info('Final Size: %r', size)
        return corner, size

    def _calculate_draw_order(self, parents):
        order = csgraph.depth_first_order(
            csgraph.reconstruct_path(self._edge_matrix, parents, directed=False),
            self.center,
            return_predecessors=False,
        )[::-1]
        log.info('Draw order: %s', ', '.join(self._images[i].name for i in order))
        return order

    def _calculate_total_homographies(self, parents):
        """Calculate the full homography each picture will have for the final image"""
        c = self.center

        # Calculate each homography from the source to the destination
        next_H = []
        for src_idx, dst_idx in enumerate(parents):
            if dst_idx < 0 or src_idx == c:
                # We are at the center node
                next_H.append(np.identity(3))
                continue
            matches = self._get_match(src_idx, dst_idx)
            swap = (src_idx, dst_idx) not in self._matches
            src, dst = self._images[src_idx], self._images[dst_idx]
            H = self._find_homography(src, dst, matches, swap=swap)
            next_H.append(H)

        # Now that we have the homographies from each to its next-to-center,
        # calculate relative to the center
        total_H = [None] * len(parents)
        total_H[c] = next_H[c]
        path = []
        while any(i is None for i in total_H):
            path.append(next(n for n, i in enumerate(total_H) if i is None))
            while path:
                src_idx = path.pop()
                dst_idx = parents[src_idx]
                if c == src_idx:
                    continue

                if total_H[dst_idx] is None:
                    # The next node needs to be calculated
                    path.extend((src_idx, dst_idx))
                else:
                    # Matrix multiply src to dst
                    total_H[src_idx] = next_H[src_idx].dot(total_H[dst_idx])
        return total_H

    def _get_match(self, src_idx: int, dst_idx: int):
        if (src_idx, dst_idx) in self._matches:
            return self._matches[(src_idx, dst_idx)]
        return self._matches[(dst_idx, src_idx)]

    def _find_homography(
            self,
            src: _StitchImage,
            dst: _StitchImage,
            matches: List[cv2.DMatch],
            swap=False) -> np.ndarray:
        """Calculate the actual homography for a perspective transform from src to dst"""
        log.info('Transforming %s -> %s', src.name, dst.name)
        if swap:
            src, dst = dst, src
            log.debug('Performing swapped homography find')

        src_data = np.array(
            [src.kp[i.queryIdx].pt for i in matches],
            dtype=np.float64).reshape(-1, 1, 2)

        dst_data = np.array(
            [dst.kp[i.trainIdx].pt for i in matches],
            dtype=np.float64).reshape(-1, 1, 2)

        if swap:
            src_data, dst_data = dst_data, src_data
            src, dst = dst, src

        H, status = cv2.findHomography(src_data, dst_data, cv2.RANSAC, 2.)
        if status.sum() == 0:
            raise ValueError('Critical error finding homography - this should not happen')
        log.debug('Homography for %s->%s:\n%s', src.name, dst.name, H)
        return H

    def _find_center(self) -> int:
        log.debug('Calculating the center image')
        shortest_path = csgraph.shortest_path(
            self._edge_matrix, directed=False,
        )
        log.debug('Shortest path result: %s', shortest_path)
        center = np.argmin(shortest_path.max(axis=1))
        log.debug('The center image is %s (index %d)' % (self._images[center].name, center))
        return center

    @property
    def _edge_matrix(self):
        if len(self._images) == 0:
            raise ValueError('Must have at least one image!')
        current = self._current_edge_matrix
        if current is not None and current.shape[0] == len(self._images):
            return current
        # guarantee same order
        all_matches = list(self._matches)
        base = max(len(v) for v in self._matches.values()) + 1
        # Score connections based on number of "good" matches
        values = [base - len(self._matches[i]) for i in all_matches]
        self._current_edge_matrix = csr.csr_matrix(
            (values, tuple(np.array(all_matches).T)),
            shape=(len(self._images), len(self._images)),
        )
        log.debug('New edge matrix:\n%s', self._current_edge_matrix.toarray())
        return self._current_edge_matrix

    def _match_features(self, src: _StitchImage, dst: _StitchImage) -> Optional[List[cv2.DMatch]]:
        """Match features between two images. Uses a ratio test to filter."""
        log.debug('Matching features of %s and %s', src.name, dst.name)
        matches = matcher.knnMatch(src.feat, dst.feat, k=2)
        # Ratio test
        good = [i for i, j in matches
                if i.distance < self.ratio_threshold * j.distance]
        if self.debug:
            imshow(
                cv2.drawMatches(
                    src.image[..., :3], src.kp,
                    dst.image[..., :3], dst.kp, good, None),
                title='%s matched with %s' % (src.name, dst.name), figsize=(10, 10)
            )
        log.debug('%d features matched, %d of which are good', len(matches), len(good))
        if len(good) >= self.matches_threshold:
            log.info('%s <=> %s (score %d)', src.name, dst.name, len(good))
            return good
        return None