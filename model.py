from enum import Enum
import gc
import numpy as np
import jax.numpy as jnp
import tomesd
import jax

# from diffusers import StableDiffusionInstructPix2PixPipeline, StableDiffusionControlNetPipeline, ControlNetModel, UNet2DConditionModel
# from diffusers.schedulers import EulerAncestralDiscreteScheduler, DDIMScheduler

from diffusers import FlaxDDIMScheduler
from diffusers import FlaxControlNetModel, FlaxStableDiffusionControlNetPipeline
# from text_to_video_pipeline import TextToVideoPipeline
from transformers import CLIPTokenizer

import utils
import gradio_utils
import os
on_huggingspace = os.environ.get("SPACE_AUTHOR_NAME") == "PAIR"


class ModelType(Enum):
    Pix2Pix_Video = 1,
    Text2Video = 2,
    ControlNetCanny = 3,
    ControlNetCannyDB = 4,
    ControlNetPose = 5,
    ControlNetDepth = 6,


class Model:
    def __init__(self, device, dtype, **kwargs):
        self.device = device
        self.dtype = dtype
        self.rng = jax.random.PRNGKey(0)
        self.pipe_dict = {
            # ModelType.Pix2Pix_Video: StableDiffusionInstructPix2PixPipeline,
            # ModelType.Text2Video: TextToVideoPipeline,
            # ModelType.ControlNetCanny: StableDiffusionControlNetPipeline,
            # ModelType.ControlNetCannyDB: StableDiffusionControlNetPipeline,
            ModelType.ControlNetPose: FlaxStableDiffusionControlNetPipeline,
            # ModelType.ControlNetDepth: StableDiffusionControlNetPipeline,
        }
        self.controlnet_attn_proc = utils.CrossFrameAttnProcessor(
            unet_chunk_size=2)
        self.pix2pix_attn_proc = utils.CrossFrameAttnProcessor(
            unet_chunk_size=3)
        self.text2video_attn_proc = utils.CrossFrameAttnProcessor(
            unet_chunk_size=2)

        self.pipe = None
        self.model_type = None

        self.states = {}
        self.model_name = ""

    def set_model(self, model_type: ModelType, model_id: str, controlnet, controlnet_params, tokenizer, **kwargs):
        if hasattr(self, "pipe") and self.pipe is not None:
            del self.pipe
            self.pipe = None
        gc.collect()
        self.pipe, self.params = FlaxStableDiffusionControlNetPipeline.from_pretrained(
                                    model_id,
                                    tokenizer=tokenizer,
                                    controlnet=controlnet,
                                    safety_checker=None,
                                    # dtype=weight_dtype,
                                    # revision=args.revision,
                                    from_pt=True,
                                )
        self.params["controlnet"] = controlnet_params
        # self.pipe = self.pipe_dict[model_type].from_pretrained(
        #     model_id, safety_checker=safety_checker, **kwargs).to(self.device).to(self.dtype)
        self.model_type = model_type
        self.model_name = model_id

    def inference_chunk(self, frame_ids, **kwargs):
        if not hasattr(self, "pipe") or self.pipe is None:
            return
        prng_seed = jax.random.split(self.rng, jax.device_count())
        prompt = np.array(kwargs.pop('prompt'))
        negative_prompt = np.array(kwargs.pop('negative_prompt', ''))
        latents = None
        if 'latents' in kwargs:
            latents = kwargs.pop('latents')[frame_ids]
        if 'image' in kwargs:
            kwargs['image'] = kwargs['image'][frame_ids]
        if 'video_length' in kwargs:
            kwargs['video_length'] = len(frame_ids)
        if self.model_type == ModelType.Text2Video:
            kwargs["frame_ids"] = frame_ids
        return self.pipe(prompt=prompt[frame_ids].tolist(),
                        params=self.params,
                        prng_seed=self.prng_seed,
                         negative_prompt=negative_prompt[frame_ids].tolist(),
                         latents=latents,
                         **kwargs)

    def inference(self, split_to_chunks=False, chunk_size=8, **kwargs):
        if not hasattr(self, "pipe") or self.pipe is None:
            return

        if "merging_ratio" in kwargs:
            merging_ratio = kwargs.pop("merging_ratio")

            # if merging_ratio > 0:
            tomesd.apply_patch(self.pipe, ratio=merging_ratio)

        if 'image' in kwargs:
            f = kwargs['image'].shape[0]
        else:
            f = kwargs['video_length']

        assert 'prompt' in kwargs
        prompt = [kwargs.pop('prompt')] * f
        negative_prompt = [kwargs.pop('negative_prompt', '')] * f

        frames_counter = 0

        # Processing chunk-by-chunk
        if split_to_chunks:
            chunk_ids = np.arange(0, f, chunk_size - 1)
            result = []
            for i in range(len(chunk_ids)):
                ch_start = chunk_ids[i]
                ch_end = f if i == len(chunk_ids) - 1 else chunk_ids[i + 1]
                frame_ids = [0] + list(range(ch_start, ch_end))
                print(f'Processing chunk {i + 1} / {len(chunk_ids)}')
                result.append(self.inference_chunk(frame_ids=frame_ids,
                                                   prompt=prompt,
                                                   negative_prompt=negative_prompt,
                                                   **kwargs).images[1:])
                frames_counter += len(chunk_ids)-1
                if on_huggingspace and frames_counter >= 80:
                    break
            result = np.concatenate(result)
            return result
        else:
            return self.pipe(prompt=prompt, negative_prompt=negative_prompt, 
                            params=self.params,
                            prng_seed=self.prng_seed, **kwargs).images

    def process_controlnet_pose(self,
                                video_path,
                                prompt,
                                chunk_size=8,
                                merging_ratio=0.0,
                                num_inference_steps=20,
                                controlnet_conditioning_scale=1.0,
                                guidance_scale=9.0,
                                seed=42,
                                eta=0.0,
                                resolution=512,
                                use_cf_attn=False,#should be True
                                save_path=None):
        print("Module Pose")
        video_path = gradio_utils.motion_to_video_path(video_path)
        if self.model_type != ModelType.ControlNetPose:
            model_id = "tuwonga/zukki_style"
            controlnet_id = "fusing/stable-diffusion-v1-5-controlnet-openpose"
            controlnet, controlnet_params = FlaxControlNetModel.from_pretrained(
                controlnet_id,
                # revision=args.controlnet_revision,
                from_pt=True,
                # dtype=jnp.float32,
            )
            tokenizer = CLIPTokenizer.from_pretrained(
            model_id, subfolder="tokenizer"
            )
            self.set_model(ModelType.ControlNetPose,
                            model_id=model_id,
                            tokenizer=tokenizer,
                            controlnet=controlnet,
                            controlnet_params=controlnet_params)
            self.pipe.scheduler = FlaxDDIMScheduler.from_config(
                self.pipe.scheduler.config)
            if use_cf_attn:
                self.pipe.unet.set_attn_processor(
                    processor=self.controlnet_attn_proc)
                self.pipe.controlnet.set_attn_processor(
                    processor=self.controlnet_attn_proc)

        video_path = gradio_utils.motion_to_video_path(
            video_path) if 'Motion' in video_path else video_path

        added_prompt = 'best quality, extremely detailed, HD, ultra-realistic, 8K, HQ, masterpiece, trending on artstation, art, smooth'
        negative_prompts = 'longbody, lowres, bad anatomy, bad hands, missing fingers, extra digit, fewer difits, cropped, worst quality, low quality, deformed body, bloated, ugly, unrealistic'

        video, fps = utils.prepare_video(
            video_path, resolution, self.device, self.dtype, False, output_fps=4)
        control = utils.pre_process_pose(
            video, apply_pose_detect=False).to(self.device).to(self.dtype)
        f, _, h, w = video.shape
        # Sample noise that we'll add to the latents
        latents_rng = jax.random.split(self.rng)
        latents = jax.random.normal(latents_rng, (1, 4, h//8, w//8))
        latents = latents.repeat(f, 1, 1, 1)
        result = self.inference(image=control,
                                prompt=prompt + ', ' + added_prompt,
                                height=h,
                                width=w,
                                negative_prompt=negative_prompts,
                                num_inference_steps=num_inference_steps,
                                guidance_scale=guidance_scale,
                                controlnet_conditioning_scale=controlnet_conditioning_scale,
                                eta=eta,
                                latents=latents,
                                output_type='numpy',
                                split_to_chunks=True,
                                chunk_size=chunk_size,
                                merging_ratio=merging_ratio,
                                )
        return utils.create_gif(result, fps, path=save_path, watermark=None)
