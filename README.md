# ComfyUI Output Images Gallery

## Introduction

ComfyUI Output Images Gallery is a simple web application built with Flask to display a gallery of images. It's designed to showcase a collection of images with thumbnails and provides an easy way for users to view and navigate through the gallery.

<img src="logo.png" width="50%" height="50%">

## Features

- Display images with responsive thumbnails.
- Modern and professional design for desktop browser and mobile.
- Pagination for easy navigation.
- Click on thumbnails to view full-sized images in a lightbox (Fancybox).
- Dark mode aesthetic for an elegant look.

## Installation and Setup

### Prerequisites

Before you begin, ensure you have the following installed:

- **Python:** You can download Python from [Python's official website](https://www.python.org/downloads/).
- **Flask:** Install Flask, the web framework, using pip:
- **Pillow:** Pillow is used for image processing. Install it using pip:

### Symlink Your Image Directory

To use your own images, symlink your image directory to the project's "static/images/output" directory:

```ln -s /path/to/your/images /path/to/gallery/static/images/output```

Configure Paths
You may need to configure the paths for the logo, thumbnails, and original images in the app.py file:

logo.png - Change the path of the logo image on line 16.
thumbnails - The generated thumbnail images will be saved in the static/thumbnails directory.
original images - The original images are expected in the static/images/output directory (symlinked from your image directory).
Set Port Number and Run the App
You can change the port number and host as needed in the app.py file on line 44. By default, it's set to run on http://0.0.0.0:9999.

Start the Flask app by running the following command in your terminal or command prompt:

```python app.py```

The app should now be running, and you can access it by opening a web browser and navigating to http://localhost:9999 (or the custom host and port you've specified).

## Local face recognition

The gallery can detect known faces with InsightFace `buffalo_l` and add the matching photo tags. Recognition is local and does not call the ComfyUI server.

### Install the optional runtime

For an NVIDIA GPU (with a compatible CUDA installation):

```powershell
python -m pip install -r requirements-face.txt
python -m pip install onnxruntime-gpu
```

For CPU-only processing, replace `onnxruntime-gpu` with `onnxruntime`. Do not install both ONNX Runtime packages in the same environment.

### Install or reuse `buffalo_l`

No model is downloaded when the gallery starts. The default expected layout is:

```text
instance/
  face_models/
    models/
      buffalo_l/
        det_10g.onnx
        w600k_r50.onnx
        ...
```

To reuse an existing ComfyUI model folder, set `FACE_MODEL_ROOT` to the directory which directly contains `models/buffalo_l`. For example:

```powershell
$env:FACE_MODEL_ROOT = "D:\ComfyUI\models\insightface"
python app.py
```

The application tries `CUDAExecutionProvider` first and falls back to `CPUExecutionProvider` when CUDA is unavailable.

### Usage

1. Open the face icon in the top toolbar.
2. Create an identity. Its name is also the photo tag that recognition will add.
3. Add 3 to 8 varied reference faces, either from an analyzed gallery photo or through the private upload form.
4. Analyze a photo, the current album, a selection, or the entire gallery.
5. Confirm or reject ambiguous results in each photo's details panel.

Existing tags are migrated as manual tags. A face rescan can remove only tags that it added automatically; it never removes a manual assignment. Uploaded references, embeddings, and job state are stored below `instance/` and are excluded from Git.

The InsightFace source code and its pretrained model weights have different license terms. The supplied `buffalo_l` weights are intended here for local non-commercial use; obtain the appropriate model license before commercial deployment.

## Local image analysis

The scan dialog can classify image sensitivity and add visual tags without sending gallery images to ComfyUI or another remote service:

- `Freepik/nsfw_image_detector` supplies the ordered `neutral`, `low`, `medium`, and `high` probabilities.
- NudeNet confirms exposed body regions and can raise the resulting sensitivity.
- `SmilingWolf/wd-swinv2-tagger-v3` adds general English tags such as clothing and visual attributes.

Install the optional runtime:

```powershell
python -m pip install -r requirements-analysis.txt
```

Install a PyTorch build and exactly one ONNX Runtime backend appropriate for the machine. The NVIDIA variant is:

```powershell
python -m pip install onnxruntime-gpu
```

The Freepik and WD SwinV2 files are downloaded automatically on the first image-analysis scan. NudeNet's default 320n model is included in its Python package. Files are cached below `instance/image_models`; set `IMAGE_MODEL_ROOT` to use another cache directory and `HF_TOKEN` when Hugging Face authentication is required.

The first analysis can therefore take several minutes. Missing dependencies or download failures are reported by the scan and do not delete an older successful analysis. A single photo can be analyzed again from its detail panel with the `Image IA` button.

Customization
You can customize the gallery's appearance and behavior by modifying the HTML templates, CSS styles, and the Flask application code. Feel free to tailor it to your specific requirements.

![image](https://github.com/Smuzzies/comfyui_image_gallery/assets/110495122/eb8adc34-811e-434b-9ea7-d225f7cc63bb)
![image](https://github.com/Smuzzies/comfyui_image_gallery/assets/110495122/cf30e7ab-041d-4b9a-99c5-b6d863bb09f8)
