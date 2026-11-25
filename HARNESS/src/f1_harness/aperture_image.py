"""Find planar aperture candidates in RGB pixels. No depth or robot pose is inferred."""
import cv2
import numpy as np


def aperture_features(rgb, *, include_notches=False):
    """Candidates in original pixels. A candidate does not establish fixture identity."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    result = {name: [] for name in ('square', 'triangle', 'circle')}
    # Both sides of a strong edge are returned. Keep just the outermost fit.
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 25, 65)
    edge_contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_OTSU)
    _, dark = cv2.threshold(gray, .5*threshold, 255, cv2.THRESH_BINARY_INV)
    dark_contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if include_notches:
        # Concavity generates U-slot hypotheses. The measured 3-D registration
        # resolves their identity, including the case with several dark notches.
        result['notch'] = []
        for contour in dark_contours:
            if not max(45, dark.size * .00015) <= cv2.contourArea(contour) <= dark.size * .075:
                continue
            polygon = cv2.approxPolyDP(contour, .025 * cv2.arcLength(contour, True), True)
            x, y, w, h = cv2.boundingRect(contour)
            if (len(polygon) > 4 and not cv2.isContourConvex(polygon)
                    and x > 1 and y > 1 and x+w < width-1 and y+h < height-1):
                result['notch'].append(dict(corners_px=polygon[:, 0].tolist(), center_px=[x+w/2, y+h/2]))
    contours = sorted((*edge_contours, *dark_contours), key=cv2.contourArea, reverse=True)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < max(45, width * height * .00015):
            break
        if area > width * height * .075:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if x < 2 or y < 2 or x + w > width - 2 or y + h > height - 2:
            continue
        if min(w, h) < .4 * max(w, h):
            continue
        # A hole has a darker interior than the surrounding lid. Reject closed
        # contours of bright highlights and wooden blocks themselves.
        pad = 4
        x0, y0 = max(0, x-pad), max(0, y-pad)
        roi = gray[y0:min(height,y+h+pad), x0:min(width,x+w+pad)]
        mask = np.zeros(roi.shape, np.uint8)
        cv2.drawContours(mask, [contour - [[[x0, y0]]]], -1, 1, cv2.FILLED)
        kernel = np.ones((5, 5), np.uint8)
        rim = cv2.dilate(mask, kernel) > mask
        interior = cv2.erode(mask, kernel).astype(bool)
        if not interior.any() or not rim.any() or np.median(roi[rim]) - float(np.median(roi[interior])) < 12:
            continue
        polygon = cv2.approxPolyDP(contour, .025*cv2.arcLength(contour, True), True)
        corners = polygon[:, 0, :]
        center = np.mean(corners, axis=0)
        if len(corners) in (3, 4) and cv2.isContourConvex(polygon):
            if not .84 < cv2.contourArea(polygon) / area < 1.10:
                continue
            sides = np.linalg.norm(corners - np.roll(corners, -1, axis=0), axis=1)
            if sides.min() < .45 * sides.max():
                continue
            name = 'triangle' if len(corners) == 3 else 'square'
            row = dict(corners_px=corners.tolist())
        elif len(corners) >= 6 and len(contour) >= 5:
            (cx, cy), axes, angle = cv2.fitEllipse(contour)
            ellipse_area = np.pi * axes[0] * axes[1] / 4
            if ellipse_area < 1 or not .90 < area / ellipse_area < 1.10 or min(axes) < .48 * max(axes):
                continue
            # Fit quality over the full boundary distinguishes a round aperture
            # from a notch whose total area merely happens to match an ellipse.
            theta = np.deg2rad(angle)
            rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
            normalized = ((contour[:, 0, :] - [cx,cy]) @ rotation) / (np.asarray(axes) / 2)
            if np.quantile(abs(np.linalg.norm(normalized, axis=1) - 1), .9) > .12:
                continue
            center = np.array([cx,cy])
            name = 'circle'
            row = dict(ellipse=[[cx,cy],list(axes),angle])
        else:
            continue
        if any(np.linalg.norm(np.asarray(v['center_px']) - center) < .16 * min(w, h)
               for v in result[name]):
            continue
        if len(result[name]) < 12:
            result[name].append(dict(row, center_px=center.tolist(), area_px=area))
    return result
