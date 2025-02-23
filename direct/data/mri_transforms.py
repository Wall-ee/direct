# coding=utf-8
# Copyright (c) DIRECT Contributors


import functools
import logging
import warnings
from typing import Any, Callable, Dict, Iterable, Optional

import numpy as np
import torch

from direct.data import transforms as T
from direct.utils import DirectModule, DirectTransform
from direct.utils.asserts import assert_complex

logger = logging.getLogger(__name__)


class Compose(DirectModule):
    """Compose several transformations together, for instance ClipAndScale and a flip.

    Code based on torchvision: https://github.com/pytorch/vision, but got forked from there as torchvision has some
    additional dependencies.
    """

    def __init__(self, transforms: Iterable) -> None:
        super().__init__()
        self.transforms = transforms

    def __call__(self, sample):
        for transform in self.transforms:
            sample = transform(sample)

        return sample

    def __repr__(self):
        repr_string = self.__class__.__name__ + "("
        for transform in self.transforms:
            repr_string += "\n"
            repr_string += f"    {transform}"
        repr_string += "\n)"
        return repr_string


# TODO: Flip augmentation
class RandomFlip(DirectTransform):
    """Random image flip.

    Not implemented yet.
    """

    def __call__(self):
        raise NotImplementedError


class CreateSamplingMask(DirectModule):
    """Data Transformer for training MRI reconstruction models.

    Creates sampling mask.
    """

    def __init__(self, mask_func, shape=None, use_seed=True, return_acs=False):
        super().__init__()
        self.mask_func = mask_func
        self.shape = shape
        self.use_seed = use_seed
        self.return_acs = return_acs

    def __call__(self, sample):
        if not self.shape:
            shape = sample["kspace"].shape[1:]  # ([slice], height, width, complex=2)
        elif any(_ is None for _ in self.shape):  # Allow None as values.
            kspace_shape = list(sample["kspace"].shape[1:-1])
            shape = tuple(_ if _ else kspace_shape[idx] for idx, _ in enumerate(self.shape)) + (2,)
        else:
            shape = self.shape + (2,)

        seed = None if not self.use_seed else tuple(map(ord, str(sample["filename"])))

        # Shape (coil, [slice], height, width, complex=2)
        sampling_mask = self.mask_func(shape=shape, seed=seed, return_acs=False)

        if sample.get("padding_left", 0) > 0 or sample.get("padding_right", 0) > 0:

            if sample["kspace"].shape[2] != shape[-2]:
                raise ValueError(
                    "Currently only support for the `width` axis to be at the 2th position when padding. "
                    + "When padding in left or right is present, you cannot crop in the phase-encoding direction!"
                )

            padding_left = sample["padding_left"]
            padding_right = sample["padding_right"]

            sampling_mask[:, :, :padding_left, :] = 0
            sampling_mask[:, :, padding_right:, :] = 0

        # Shape (1, [slice], height, width, 1)
        sample["sampling_mask"] = sampling_mask

        if self.return_acs:
            kspace_shape = sample["kspace"].shape[1:]
            sample["acs_mask"] = self.mask_func(shape=kspace_shape, seed=seed, return_acs=True)

        return sample


class CropAndMask(DirectModule):
    """Data Transformer for training MRI reconstruction models.

    Crops and Masks kspace using sampling mask.
    """

    def __init__(
        self,
        crop,
        use_seed=True,
        forward_operator=T.fft2,
        backward_operator=T.ifft2,
        image_space_center_crop=False,
        random_crop_sampler_type="uniform",
    ):
        """
        Parameters
        ----------
        crop: tuple or None
            Size to crop input_image to.
        mask_func: direct.common.subsample.MaskFunc
            A function which creates a mask of the appropriate shape.
        use_seed: bool
            If true, a pseudo-random number based on the filename is computed so that every slice of the volume get
            the same mask every time.
        forward_operator: callable
            The __call__ operator, e.g. some form of FFT (centered or uncentered).
        backward_operator: callable
            The backward operator, e.g. some form of inverse FFT (centered or uncentered).
        image_space_center_crop: bool
            If set, the crop in the data will be taken in the center
        random_crop_sampler_type: str
            If "uniform" the random cropping will be done by uniformly sampling `crop`, as opposed to `gaussian` which
            will sample from a gaussian distribution.
        """
        super().__init__()
        self.logger = logging.getLogger(type(self).__name__)

        self.use_seed = use_seed
        self.image_space_center_crop = image_space_center_crop

        self.crop = crop
        self.crop_func = None
        self.random_crop_sampler_type = random_crop_sampler_type
        if self.crop:
            if self.image_space_center_crop:
                self.crop_func = T.complex_center_crop
            else:
                self.crop_func = functools.partial(T.complex_random_crop, sampler=self.random_crop_sampler_type)

        self.forward_operator = forward_operator
        self.backward_operator = backward_operator

        self.image_space_center_crop = image_space_center_crop

    def __call__(self, sample: Dict[str, Any]):
        """

        Parameters
        ----------
        sample: dict

        Returns
        -------
        data dictionary
        """
        # Shape (coil, [slice], height, width, complex=2)
        kspace = sample["kspace"]

        # Image-space croppable objects
        croppable_images = ["sensitivity_map", "input_image"]

        # Shape (coil, [slice], height, width, complex=2) if not None
        sensitivity_map = sample.get("sensitivity_map", None)
        # Shape (1, [slice], height, width, 1)
        sampling_mask = sample["sampling_mask"]
        # Shape (coil, [slice], height, width, complex=2)
        backprojected_kspace = self.backward_operator(kspace)

        # TODO: Also create a kspace-like crop function
        if self.crop:
            backprojected_kspace = self.crop_func(
                [backprojected_kspace],
                self.crop,
                contiguous=True,
            )
            # Compute new k-space for the cropped input_image
            kspace = self.forward_operator(backprojected_kspace)
            for key in croppable_images:
                if key in sample:
                    sample[key] = self.crop_func(
                        [sample[key]],
                        self.crop,
                        contiguous=True,
                    )
            # TODO(gy): This is not correct, since cropping is done in the image space.
            sampling_mask = self.crop_func(
                [sampling_mask],
                self.crop,
                contiguous=True,
            )
        masked_kspace, sampling_mask = T.apply_mask(kspace, sampling_mask)
        # Shape ([slice], height, width)
        sample["target"] = T.root_sum_of_squares(backprojected_kspace, dim=0)
        # Shape (coil, [slice], height, width, complex=2)
        sample["masked_kspace"] = masked_kspace
        # Shape (1, [slice], height, width, 1)
        sample["sampling_mask"] = sampling_mask
        # Shape (coil, [slice], height, width, complex=2)
        sample["kspace"] = kspace  # The cropped kspace

        if sensitivity_map is not None:
            sample["sensitivity_map"] = sensitivity_map

        return sample


class ComputeImage(DirectModule):
    """Compute Image transform.

    Type of accepted reconstructions: "complex"
    """

    def __init__(self, kspace_key, target_key, backward_operator, type_reconstruction="complex"):
        super().__init__()
        self.backward_operator = backward_operator
        self.kspace_key = kspace_key
        self.target_key = target_key

        self.type_reconstruction = type_reconstruction

        if type_reconstruction.lower() not in ["complex", "sense", "rss"]:
            raise ValueError(
                f"Only `complex`, `rss` and `sense` are possible choices for `reconstruction_type`. "
                f"Got {self.type_reconstruction}."
            )

    def __call__(self, sample, coil_dim=0, spatial_dims=(1, 2)):
        """
        Parameters
        ----------
        sample: dict
            Contains key kspace_key with value a torch.Tensor of shape (coil, *spatial_dims, complex=2).
        coil_dim: int
            Coil dimension. Default: 0.
        spatial_dims: (int, int)
            Spatial dimensions corresponding to (height, width). Default: (1, 2).

        Returns
        ----------
        sample: dict
            Contains key target_key with value a torch.Tensor of shape (*spatial_dims) or (*spatial_dims) if
            type_reconstruction is 'rss'.
        """
        kspace_data = sample[self.kspace_key]

        # Get complex-valued data solution
        image = self.backward_operator(kspace_data, dim=spatial_dims)
        if self.type_reconstruction == "complex":
            sample[self.target_key] = image.sum(coil_dim)
        elif self.type_reconstruction.lower() == "rss":
            sample[self.target_key] = T.root_sum_of_squares(image, dim=coil_dim)
        elif self.type_reconstruction == "sense":
            if "sensitivity_map" not in sample:
                raise ValueError("Sensitivity map is required for SENSE reconstruction.")
            sample[self.target_key] = T.complex_multiplication(T.conjugate(sample["sensitivity_map"]), image).sum(
                coil_dim
            )

        return sample


class EstimateBodyCoilImage(DirectModule):
    """Estimates body coil image."""

    def __init__(self, mask_func, backward_operator, use_seed=True):
        super().__init__()
        self.mask_func = mask_func
        self.use_seed = use_seed
        self.backward_operator = backward_operator

    def __call__(self, sample, coil_dim=0):
        kspace = sample["kspace"]
        # We need to create an ACS mask based on the shape of this kspace, as it can be cropped.

        seed = None if not self.use_seed else tuple(map(ord, str(sample["filename"])))
        kspace_shape = sample["kspace"].shape[1:]
        acs_mask = self.mask_func(shape=kspace_shape, seed=seed, return_acs=True)

        kspace = acs_mask * kspace + 0.0
        acs_image = self.backward_operator(kspace)

        sample["body_coil_image"] = T.root_sum_of_squares(acs_image, dim=coil_dim)
        return sample


class EstimateSensitivityMap(DirectModule):
    """Data Transformer for training MRI reconstruction models.

    Estimates sensitivity maps given kspace data.
    """

    def __init__(
        self,
        kspace_key: str = "kspace",
        backward_operator: Callable = T.ifft2,
        type_of_map: Optional[str] = "unit",
        gaussian_sigma: Optional[float] = None,
    ) -> None:
        super().__init__()
        self.backward_operator = backward_operator
        self.kspace_key = kspace_key
        self.type_of_map = type_of_map
        self.gaussian_sigma = gaussian_sigma

    def estimate_acs_image(self, sample):
        """Estimates ACS image."""
        # Shape (coil, [slice], height, width, complex=2)
        kspace_data = sample[self.kspace_key]

        if kspace_data.shape[0] == 1:
            warnings.warn(
                "Estimation of sensitivity map of Single-coil data. This warning will be displayed only once."
            )

        if "sensitivity_map" in sample:
            warnings.warn(
                "`sensitivity_map` is given, but will be overwritten. This warning will be displayed only once."
            )

        if self.gaussian_sigma == 0 or not self.gaussian_sigma:
            kspace_acs = kspace_data * sample["acs_mask"] + 0.0  # + 0.0 removes the sign of zeros.
        else:
            width_dim = -2
            gaussian_mask = torch.linspace(-1, 1, kspace_data.size(width_dim), dtype=kspace_data.dtype)
            gaussian_mask = torch.exp(-((gaussian_mask / self.gaussian_sigma) ** 2))
            gaussian_mask = gaussian_mask.reshape(
                (1, 1, gaussian_mask.shape[0], 1)
                if len(kspace_data.shape) == 4
                else (1, 1, 1, gaussian_mask.shape[0], 1)
            )
            kspace_acs = kspace_data * sample["acs_mask"] * gaussian_mask + 0.0

        # Get complex-valued data solution
        # Shape (coil, [slice], height, width, complex=2)
        acs_image = self.backward_operator(kspace_acs)

        return acs_image

    def __call__(self, sample, coil_dim=0):
        """Calculates sensitivity maps for the input sample.

        Parameters
        ----------
        sample: dict
            Must contain key matching kspace_key with value a (complex) torch.Tensor
            of shape (coil, height, width, complex=2).
        coil_dim: int
            Coil dimension. Default: 0.
        Returns
        ----------
        sample: dict
        """
        if self.type_of_map == "unit":
            kspace = sample[self.kspace_key]
            sensitivity_map = torch.zeros(kspace.shape).float()
            # Assumes complex channel is last
            assert_complex(kspace, complex_last=True)
            sensitivity_map[..., 0] = 1.0
            # Shape (coil, [slice], height, width, complex=2)
            sample["sensitivity_map"] = sensitivity_map.to(kspace.device)

        elif self.type_of_map == "rss_estimate":
            # Shape (coil, [slice], height, width, complex=2)
            acs_image = self.estimate_acs_image(sample)
            # Shape ([slice], height, width)
            acs_image_rss = T.root_sum_of_squares(acs_image, dim=coil_dim)
            # Shape (1, [slice], height, width, 1)
            acs_image_rss = acs_image_rss.unsqueeze(0).unsqueeze(-1)
            # Shape (coil, [slice], height, width, complex=2)
            sample["sensitivity_map"] = T.safe_divide(acs_image, acs_image_rss)
        else:
            raise ValueError(f"Expected type of map to be either `unit` or `rss_estimate`. Got {self.type_of_map}.")

        return sample


class DeleteKeys(DirectModule):
    """Remove keys from the sample if present."""

    def __init__(self, keys):
        super().__init__()
        self.keys = keys

    def __call__(self, sample):
        for key in self.keys:
            if key in sample:
                del sample[key]

        return sample


class PadCoilDimension(DirectModule):
    """Pad the coils by zeros to a given number of coils.

    Useful if you want to collate volumes with different coil dimension.
    """

    def __init__(self, pad_coils: Optional[int] = None, key: str = "masked_kspace"):
        """
        Parameters
        ----------
        pad_coils: int
            Number of coils to pad to.
        key: tuple
            Key to pad in sample
        """
        super().__init__()
        self.num_coils = pad_coils
        self.key = key

    def __call__(self, sample, coil_dim=0):
        if not self.num_coils:
            return sample

        if self.key not in sample:
            return sample

        data = sample[self.key]

        curr_num_coils = data.shape[coil_dim]
        if curr_num_coils > self.num_coils:
            raise ValueError(
                f"Tried to pad to {self.num_coils} coils, but already have {curr_num_coils} for "
                f"{sample['filename']}."
            )
        if curr_num_coils == self.num_coils:
            return sample

        shape = data.shape
        num_coils = shape[coil_dim]
        padding_data_shape = list(shape).copy()
        padding_data_shape[coil_dim] = max(self.num_coils - num_coils, 0)
        zeros = torch.zeros(padding_data_shape, dtype=data.dtype)
        sample[self.key] = torch.cat([zeros, data], dim=coil_dim)

        return sample


class Normalize(DirectModule):
    """Normalize the input data either to the percentile or to the maximum."""

    def __init__(self, normalize_key="masked_kspace", percentile=0.99):
        """

        Parameters
        ----------
        normalize_key: str
            Key name to compute the data for. If the maximum has to be computed on the ACS, ensure the reconstruction
            on the ACS is available (typically `body_coil_image`).
        percentile: float or None
            Rescale data with the given percentile. If None, the division is done by the maximum.
        """
        super().__init__()
        self.normalize_key = normalize_key
        self.percentile = percentile

        self.other_keys = [
            "masked_kspace",
            "target",
            "kspace",
            "body_coil_image",  # sensitivity_map does not require normalization.
            "initial_image",
            "initial_kspace",
        ]

    def __call__(self, sample):
        if self.normalize_key == "scaling_factor":  # This is a real-valued given number
            scaling_factor = sample["scaling_factor"]
        elif not self.normalize_key:
            scaling_factor = 1.0
        else:
            data = sample[self.normalize_key]

            # Compute the maximum and scale the input
            if self.percentile:
                tview = -1.0 * T.modulus(data).view(-1)
                scaling_factor, _ = torch.kthvalue(tview, int((1 - self.percentile) * tview.size()[0]) + 1)
                scaling_factor = -1.0 * scaling_factor
            else:
                scaling_factor = T.modulus(data).max()

        # Normalize data
        if self.normalize_key:
            for key in sample.keys():
                if key != self.normalize_key and key not in self.other_keys:
                    continue
                sample[key] = sample[key] / scaling_factor

        sample["scaling_diff"] = 0.0
        sample["scaling_factor"] = scaling_factor
        return sample


class WhitenData(DirectModule):
    """Whitens complex data."""

    def __init__(self, epsilon=1e-10, key="complex_image"):
        super().__init__()
        self.epsilon = epsilon
        self.key = key

    def complex_whiten(self, complex_image):
        """Whiten complex image."""
        # From: https://github.com/facebookresearch/fastMRI
        #       blob/da1528585061dfbe2e91ebbe99a5d4841a5c3f43/banding_removal/fastmri/data/transforms.py#L464  # noqa
        real = complex_image[..., 0]
        imag = complex_image[..., 1]

        # Center around mean.
        mean = complex_image.mean()
        centered_complex_image = complex_image - mean

        # Determine covariance between real and imaginary.
        n_elements = real.nelement()
        real_real = (real.mul(real).sum() - real.mean().mul(real.mean())) / n_elements
        real_imag = (real.mul(imag).sum() - real.mean().mul(imag.mean())) / n_elements
        imag_imag = (imag.mul(imag).sum() - imag.mean().mul(imag.mean())) / n_elements
        eig_input = torch.Tensor([[real_real, real_imag], [real_imag, imag_imag]])

        # Remove correlation by rotating around covariance eigenvectors.
        eig_values, eig_vecs = torch.eig(eig_input, eigenvectors=True)

        # Scale by eigenvalues for unit variance.
        std = (eig_values[:, 0] + self.epsilon).sqrt()
        whitened_image = torch.matmul(centered_complex_image, eig_vecs) / std

        return mean, std, whitened_image

    def __call__(self, sample):
        _, _, whitened_image = self.complex_whiten(sample[self.key])
        sample[self.key] = whitened_image


class ToTensor:
    """Transforms all np.array-like values in sample to torch.tensors."""

    def __call__(self, sample):
        """
        Parameters
        ----------
        sample: dict
             Contains key 'kspace' with value a np.array of shape (coil, height, width) (2D)
             or (coil, slice, height, width) (3D)
        Returns
        -------
        sample: dict
             Contains key 'kspace' with value a torch.Tensor of shape (coil, height, width) (2D)
             or (coil, slice, height, width) (3D)
        """

        ndim = sample["kspace"].ndim - 1

        if ndim not in [2, 3]:
            raise ValueError(f"Can only cast 2D and 3D data (+coil) to tensor. Got {ndim}.")

        # Shape:    2D: (coil, height, width, complex=2), 3D: (coil, slice, height, width, complex=2)
        sample["kspace"] = T.to_tensor(sample["kspace"]).float()
        # Sensitivity maps are not necessarily available in the dataset.
        if "initial_kspace" in sample:
            # Shape:    2D: (coil, height, width, complex=2), 3D: (coil, slice, height, width, complex=2)
            sample["initial_kspace"] = T.to_tensor(sample["initial_kspace"]).float()
        if "initial_image" in sample:
            # Shape:    2D: (height, width), 3D: (slice, height, width)
            sample["initial_image"] = T.to_tensor(sample["initial_image"]).float()

        if "sensitivity_map" in sample:
            # Shape:    2D: (coil, height, width, complex=2), 3D: (coil, slice, height, width, complex=2)
            sample["sensitivity_map"] = T.to_tensor(sample["sensitivity_map"]).float()
        if "target" in sample:
            # Shape:    2D: (coil, height, width), 3D: (coil, slice, height, width)
            sample["target"] = sample["target"]
        if "sampling_mask" in sample:
            sample["sampling_mask"] = torch.from_numpy(sample["sampling_mask"]).byte()
        if "acs_mask" in sample:
            sample["acs_mask"] = torch.from_numpy(sample["acs_mask"])
        if "scaling_factor" in sample:
            sample["scaling_factor"] = torch.tensor(sample["scaling_factor"]).float()
        if "loglikelihood_scaling" in sample:
            # Shape: (coil, )
            sample["loglikelihood_scaling"] = torch.from_numpy(np.asarray(sample["loglikelihood_scaling"])).float()

        return sample


def build_mri_transforms(
    forward_operator: Callable,
    backward_operator: Callable,
    mask_func: Optional[Callable],
    crop: Optional[int] = None,
    crop_type: Optional[str] = None,
    image_center_crop: bool = False,
    estimate_sensitivity_maps: bool = True,
    estimate_body_coil_image: bool = False,
    sensitivity_maps_gaussian: Optional[float] = None,
    pad_coils: Optional[int] = None,
    scaling_key: str = "scaling_factor",
    use_seed: bool = True,
) -> object:
    """Build transforms for MRI.

    - Converts input to (complex-valued) tensor.
    - Adds a sampling mask if `mask_func` is defined.
    - Adds coil sensitivities and / or the body coil_image
    - Crops the input data if needed and masks the fully sampled k-space.
    - Add a target.
    - Normalize input data.
    - Pads the coil dimension.

    Parameters
    ----------
    backward_operator: callable
    forward_operator: callable
    mask_func: callable or none
    crop: int or none
    crop_type: str or None
        Type of cropping, either "gaussian" or "uniform".
    image_center_crop: bool
    estimate_sensitivity_maps: bool
    estimate_body_coil_image: bool
    sensitivity_maps_gaussian: float
        Optional sigma for gaussian weighting of sensitivity map.
    pad_coils: int
        Number of coils to pad data to.
    scaling_key: str
        Key to use to compute scaling factor for.
    use_seed: bool

    Returns
    -------
    object: Callable
        A transformation object.
    """
    # TODO: Use seed

    mri_transforms = [ToTensor()]
    if mask_func:
        mri_transforms += [
            CreateSamplingMask(
                mask_func,
                shape=crop,
                use_seed=use_seed,
                return_acs=estimate_sensitivity_maps,
            )
        ]

    mri_transforms += [
        EstimateSensitivityMap(
            kspace_key="kspace",
            backward_operator=backward_operator,
            type_of_map="unit" if not estimate_sensitivity_maps else "rss_estimate",
            gaussian_sigma=sensitivity_maps_gaussian,
        ),
        DeleteKeys(keys=["acs_mask"]),
        CropAndMask(
            crop,
            forward_operator=forward_operator,
            backward_operator=backward_operator,
            image_space_center_crop=image_center_crop,
            random_crop_sampler_type=crop_type,
        ),
    ]
    if estimate_body_coil_image and mask_func is not None:
        mri_transforms.append(EstimateBodyCoilImage(mask_func, backward_operator=backward_operator, use_seed=use_seed))

    mri_transforms += [
        Normalize(
            normalize_key=scaling_key,
            percentile=0.99,
        ),
        PadCoilDimension(pad_coils=pad_coils, key="masked_kspace"),
        PadCoilDimension(pad_coils=pad_coils, key="sensitivity_map"),
        DeleteKeys(keys=["kspace"]),
    ]

    mri_transforms = Compose(mri_transforms)
    return mri_transforms
