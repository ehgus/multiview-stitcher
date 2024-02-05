import itertools
import warnings
from itertools import product

import dask.array as da
import numpy as np
import spatial_image as si
import xarray as xr
from dask import delayed

from multiview_stitcher import (
    param_utils,
    spatial_image_utils,
    transformation,
    weights,
)


def fusion_method_weighted_average(
    transformed_views,
    blending_weights,
    fusion_weights=None,
    params=None,
):
    """
    Simple weighted average fusion.

    Parameters
    ----------
    transformed_views : list of ndarrays
        transformed input views
    blending_weights : list of ndarrays
        blending weights for each view
    fusion_weights : list of ndarrays, optional
        additional view weights for fusion, e.g. contrast weighted scores.
        By default None.
    params : _type_, optional

    Returns
    -------
    ndarray
        Fusion of input views
    """

    if fusion_weights is None:
        additive_weights = blending_weights
    else:
        additive_weights = blending_weights * fusion_weights

    additive_weights = weights.normalize_weights(additive_weights)

    product = transformed_views * additive_weights

    return np.nansum(product, axis=0).astype(transformed_views.dtype)


def fuse(
    sims: list,
    transform_key: str = None,
    fusion_method=fusion_method_weighted_average,
    weights_method=None,
    weights_method_kwargs=None,
    output_spacing=None,
    output_stack_mode="union",
    output_origin=None,
    output_shape=None,
    output_stack_properties=None,
    output_chunksize=512,
):
    """

    Fuse input views.

    This function fuses all (Z)YX views ("fields") contained in the
    input list of images, which can additionally contain C and T dimensions.

    Parameters
    ----------
    sims : list of SpatialImage
        Input views.
    transform_key : str, optional
        Which (extrinsic coordinate system) to use as transformation parameters.
        By default None (intrinsic coordinate system).
    fusion_method : func, optional
        Fusion function to be applied. This function receives the following
        inputs (as arrays if applicable): transformed_views, blending_weights, fusion_weights, params.
        By default fusion_method_weighted_average
    weights_method : func, optional
        Function to calculate fusion weights. This function receives the
        following inputs: transformed_views (as spatial images), params.
        It returns (non-normalized) fusion weights for each view.
        By default None.
    output_spacing : dict, optional
        Spacing of the fused image for each spatial dimension, by default None
    output_stack_mode : str, optional
        Mode to determine output stack properties. Can be one of
        "union", "intersection", "sample". By default "union"
    output_origin : dict, optional
        Origin of the fused image for each spatial dimension, by default None
    output_shape : _type_, optional
        Shape of the fused image for each spatial dimension, by default None
    output_stack_properties : dict, optional
        Dictionary describing the output stack with keys
        'spacing', 'origin', 'shape'. Other output_* are ignored
        if this argument is present.
    output_chunksize : int, optional
        Chunksize of the dask data array of the fused image, by default 512

    Returns
    -------
    SpatialImage
        Fused image.
    """

    sdims = spatial_image_utils.get_spatial_dims_from_sim(sims[0])
    nsdims = [dim for dim in sims[0].dims if dim not in sdims]

    params = [
        spatial_image_utils.get_affine_from_sim(
            sim, transform_key=transform_key
        )
        for sim in sims
    ]

    params = [param_utils.invert_xparams(param) for param in params]

    if output_stack_properties is None:
        if output_spacing is None:
            output_spacing = spatial_image_utils.get_spacing_from_sim(sims[0])

        output_stack_properties = calc_fusion_stack_properties(
            sims,
            params=params,
            spacing=output_spacing,
            mode=output_stack_mode,
        )

        if output_origin is not None:
            output_stack_properties = output_origin

        if output_shape is not None:
            output_stack_properties["shape"] = output_shape

    xds = xr.Dataset(
        # For python >= 3.9 we can use the union '|' operator to merge to dict
        {
            **{(view, "sim"): sims[view] for view in range(len(sims))},
            **{(view, "param"): params[view] for view in range(len(sims))},
        },
    )

    merges = []
    for ns_coords in itertools.product(
        *tuple([xds.coords[nsdim] for nsdim in nsdims])
    ):
        sim_coord_dict = {
            ndsim: ns_coords[i] for i, ndsim in enumerate(nsdims)
        }
        params_coord_dict = {
            ndsim: ns_coords[i]
            for i, ndsim in enumerate(nsdims)
            if ndsim in params[0].dims
        }

        ssims = [sim.sel(sim_coord_dict) for sim in sims]
        sparams = [param.sel(params_coord_dict) for param in params]

        # convert ssims into dask arrays + metadata to get them
        # through fuse_field without triggering compute
        # https://dask.discourse.group/t/passing-dask-objects-to-delayed-computations-without-triggering-compute/1441
        sims_datas = [
            delayed(da.Array)(
                ssim.data.dask,
                ssim.data.name,
                ssim.data.chunks,
                ssim.data.dtype,
            )
            for ssim in ssims
        ]

        sims_metas = [
            {
                "dims": ssim.dims,
                "scale": spatial_image_utils.get_spacing_from_sim(ssim),
                "translation": spatial_image_utils.get_origin_from_sim(ssim),
            }
            for ssim in ssims
        ]

        merge_d = delayed(fuse_field)(
            sims_datas,
            sims_metas,
            sparams,
            fusion_method=fusion_method,
            weights_method=weights_method,
            weights_method_kwargs=weights_method_kwargs,
            output_stack_properties=output_stack_properties,
            output_chunksize=output_chunksize,
        )

        # continue working with dask array
        merge_data = da.from_delayed(
            delayed(lambda x: x.data)(merge_d),
            shape=[output_stack_properties["shape"][dim] for dim in sdims],
            dtype=sims[0].dtype,
        )

        # rechunk to get a chunked dask array from the delayed object
        # (hacky, is there a better way to do this?)
        merge_data = merge_data.rechunk([output_chunksize] * len(sdims))

        # trigger compute here
        merge_data = merge_data.map_blocks(
            lambda x: x.compute(),
            dtype=sims[0].dtype,
        )

        merge = si.to_spatial_image(
            merge_data,
            dims=sdims,
            scale=output_stack_properties["spacing"],
            translation=output_stack_properties["origin"],
        )

        merge = merge.expand_dims(nsdims)
        merge = merge.assign_coords(
            {ns_coord.name: [ns_coord.values] for ns_coord in ns_coords}
        )
        merges.append(merge)

    if len(merges) > 1:
        # suppress pandas future warning occuring within xarray.concat
        with warnings.catch_warnings():
            warnings.simplefilter(action="ignore", category=FutureWarning)

            # if sims are named, combine_by_coord returns a dataset
            res = xr.combine_by_coords([m.rename(None) for m in merges])
    else:
        res = merge

    res = spatial_image_utils.get_sim_from_xim(res)
    spatial_image_utils.set_sim_affine(
        res,
        param_utils.identity_transform(len(sdims), res.coords["t"]),
        transform_key,
    )

    return res


def fuse_field(
    sims_datas,
    sims_metas,
    params,
    fusion_method=fusion_method_weighted_average,
    weights_method=None,
    weights_method_kwargs=None,
    output_stack_properties=None,
    output_chunksize=512,
):
    """
    Fuse tiles from a single timepoint and channel (a (Z)YX "field").

    Parameters
    ----------
    sims : list of SpatialImage
        Input images containing only spatial dimensions.
    params : list of xarray.DataArray
        Transformation parameters for each view.
    fusion_method : func, optional
        See docstring of `fuse`.
    weights_method : func, optional
        See docstring of `fuse`.
    output_stack_properties : dict, optional
        Dictionary describing the output stack with keys
        'spacing', 'origin', 'shape'.
    output_chunksize : int, optional
       See docstring of `fuse`.

    Returns
    -------
    SpatialImage
        Fused image of the field.
    """

    # reassemble sims from data and metadata
    # this way we can pass them to fuse_field without triggering compute
    sims = [
        si.to_spatial_image(
            sim_data,
            dims=sim_meta["dims"],
            scale=sim_meta["scale"],
            translation=sim_meta["translation"],
        )
        for sim_meta, sim_data in zip(sims_metas, sims_datas)
    ]

    # reassemble sims from data and metadata
    # this way we can pass them to fuse_field without triggering compute
    sims = [
        si.to_spatial_image(
            sim_data,
            dims=sim_meta["dims"],
            scale=sim_meta["scale"],
            translation=sim_meta["translation"],
        )
        for sim_meta, sim_data in zip(sims_metas, sims_datas)
    ]

    if weights_method_kwargs is None:
        weights_method_kwargs = {}

    input_dtype = sims[0].dtype
    ndim = spatial_image_utils.get_ndim_from_sim(sims[0])
    spatial_dims = spatial_image_utils.get_spatial_dims_from_sim(sims[0])

    # get blending weights
    blending_widths = [10] * 2 if ndim == 2 else [3] + [10] * 2
    field_ws = []
    for sim in sims:
        field_w = xr.zeros_like(sim)
        field_w.data = weights.get_smooth_border_weight_from_shape(
            sim.shape[-ndim:],
            chunks=sim.chunks,
            widths=blending_widths,
        )
        field_ws.append(field_w)

    ###
    # Build output array
    ###

    # calculate output array properties
    normalized_chunks = da.core.normalize_chunks(
        [output_chunksize] * ndim,
        tuple([output_stack_properties["shape"][dim] for dim in spatial_dims]),
    )

    block_indices = product(*(range(len(bds)) for bds in normalized_chunks))
    block_offsets = [np.cumsum((0,) + bds[:-1]) for bds in normalized_chunks]
    numblocks = [len(bds) for bds in normalized_chunks]

    fused_blocks = np.empty(numblocks, dtype=object)
    for _ib, block_ind in enumerate(block_indices):
        out_chunk_shape = [
            normalized_chunks[dim][block_ind[dim]] for dim in range(ndim)
        ]
        out_chunk_offset = [
            block_offsets[dim][block_ind[dim]] for dim in range(ndim)
        ]

        out_chunk_edges = np.array(
            list(np.ndindex(tuple([2] * ndim)))
        ) * np.array(out_chunk_shape) + np.array(out_chunk_offset)

        out_chunk_edges_phys = np.array(
            [output_stack_properties["origin"][dim] for dim in spatial_dims]
        ) + np.array(out_chunk_edges) * np.array(
            [output_stack_properties["spacing"][dim] for dim in spatial_dims]
        )

        empty_chunk = True
        field_ims_t, field_ws_t = [], []

        # for each block, add contributing chunks from each input view
        for iview, (param, sim) in enumerate(zip(params, sims)):
            matrix = np.array(param[:ndim, :ndim])
            offset = np.array(param[:ndim, ndim])

            # map output chunk edges onto input image coordinates
            # to define the input region relevant for the current chunk
            rel_image_edges = np.dot(matrix, out_chunk_edges_phys.T).T + offset

            rel_image_i = np.min(rel_image_edges, 0) - np.array(
                [
                    output_stack_properties["spacing"][dim]
                    for dim in spatial_dims
                ]
            )
            rel_image_f = np.max(rel_image_edges, 0) + np.array(
                [
                    output_stack_properties["spacing"][dim]
                    for dim in spatial_dims
                ]
            )

            maps_outside = np.max(
                [
                    rel_image_i[idim] > sim.coords[dim][-1]
                    or rel_image_f[idim] < sim.coords[dim][0]
                    for idim, dim in enumerate(spatial_dims)
                ]
            )

            if maps_outside:
                fused_blocks[block_ind] = da.zeros(
                    out_chunk_shape, dtype=input_dtype
                )
                continue

            sim_reduced = sim.sel(
                {
                    dim: slice(rel_image_i[idim], rel_image_f[idim])
                    for idim, dim in enumerate(spatial_dims)
                }
            )

            empty_chunk = False

            chunk_output_stack_properties = {
                "spacing": output_stack_properties["spacing"],
                "origin": {
                    dim: output_stack_properties["origin"][dim]
                    + out_chunk_offset[idim]
                    * output_stack_properties["spacing"][dim]
                    for idim, dim in enumerate(spatial_dims)
                },
                "shape": {
                    dim: out_chunk_shape[idim]
                    for idim, dim in enumerate(spatial_dims)
                },
            }

            field_ims_t.append(
                transformation.transform_sim(
                    sim_reduced,
                    param,
                    output_chunksize=tuple(out_chunk_shape),
                    output_stack_properties=chunk_output_stack_properties,
                    order=1,
                ).data
            )

            field_ws_t.append(
                transformation.transform_sim(
                    field_ws[iview],
                    param,
                    output_chunksize=tuple(out_chunk_shape),
                    output_stack_properties=chunk_output_stack_properties,
                    order=1,
                ).data
            )

        if empty_chunk:
            continue

        field_ims_t = da.stack(field_ims_t)
        field_ws_t = da.stack(field_ws_t)

        wsum = da.nansum(field_ws_t, axis=0)
        wsum[wsum == 0] = 1

        field_ws_t = field_ws_t / wsum
        field_ws_t = weights.normalize_weights(field_ws_t)

        # calculate fusion weights
        if weights_method is not None:
            fusion_weights = weights_method(
                field_ims_t,
                field_ws_t,
            )
        else:
            fusion_weights = None

        fused_field_chunk = delayed(fusion_method)(
            field_ims_t,
            field_ws_t,
            fusion_weights,
            params,
        )

        fused_field_chunk = delayed(lambda x: np.array(x).astype(input_dtype))(
            fused_field_chunk
        )

        fused_field_chunk = da.from_delayed(
            fused_field_chunk,
            shape=tuple(out_chunk_shape),
            dtype=input_dtype,
        )

        fused_blocks[block_ind] = fused_field_chunk

    fused_field = da.block(
        fused_blocks.tolist(), allow_unknown_chunksizes=False
    )

    fused_field = si.to_spatial_image(
        fused_field,
        dims=spatial_dims,
        scale=output_stack_properties["spacing"],
        translation=output_stack_properties["spacing"],
    )

    return fused_field


def calc_fusion_stack_properties(
    sims,
    params,
    spacing,
    mode="union",
):
    """
    Calculate fusion stack properties from input views
    and transformation parameters.

    Parameters
    ----------
    sims : list of SpatialImage
        Input views.
    params : list of xarray.DataArray
        Transformation parameters for each view.
    spacing : ndarray
        Spacing of the output stack.
    mode : str, optional
        'union': output stack entirely contains all (transformed) input views
        'intersection': output stack entirely contains the intersection of
            all (transformed) input views.
        'sample': output stack contains the front (z=0) plane of each input view.
        By default "union".

    Returns
    -------
    dict
        Stack properties (shape, spacing, origin).
    """
    spatial_dims = spatial_image_utils.get_spatial_dims_from_sim(sims[0])

    views_props = [
        spatial_image_utils.get_stack_properties_from_sim(sim, asarray=False)
        for sim in sims
    ]

    params_ds = xr.Dataset(dict(enumerate(params)))

    if "t" in params_ds.dims:
        stack_properties = combine_stack_props(
            [
                calc_stack_properties_from_view_properties_and_params(
                    views_props,
                    [
                        params_ds.sel(t=t).data_vars[ip].data
                        for ip in range(len(params))
                    ],
                    spacing=spacing,
                    mode=mode,
                )
                for t in params_ds.coords["t"]
            ]
        )

    else:
        stack_properties = (
            calc_stack_properties_from_view_properties_and_params(
                views_props,
                [p.data for p in params],
                spacing=spacing,
                mode=mode,
            )
        )

    # return properties in dict form
    stack_properties = {
        k: {dim: v[idim] for idim, dim in enumerate(spatial_dims)}
        for k, v in stack_properties.items()
    }

    return stack_properties


def calc_stack_properties_from_view_properties_and_params(
    views_props,
    params,
    spacing,
    mode="union",
):
    """
    Calculate fusion stack properties.

    Parameters
    ----------
    views_props : list of dict
        Stack properties of input views.
    params : list of xarray.DataArray
        Transformation parameters for each view.
    spacing : ndarray
        Spacing of the output stack.
    mode : str, optional
        'union': output stack entirely contains all (transformed) input views
        'intersection': output stack entirely contains the intersection of
            all (transformed) input views.
        'sample': output stack contains the front (z=0) plane of each input view.
        By default "union".

    Returns
    -------
    dict
        Stack properties (shape, spacing, origin).
    """
    spatial_dims = ["z", "y", "x"][-len(spacing) :]

    # transform into array form
    spacing = np.array([spacing[dim] for dim in spatial_dims])
    views_props = [
        {k: np.array([v[dim] for dim in spatial_dims]) for k, v in vp.items()}
        for vp in views_props
    ]

    spacing = np.array(spacing).astype(float)
    ndim = len(spacing)

    stack_vertices = np.array(list(np.ndindex(tuple([2] * ndim)))).astype(
        float
    )

    if mode == "sample":
        zero_z_face_vertices = stack_vertices[
            np.where(stack_vertices[:, 0] == 0)
        ]
        zero_z_face_vertices[:, 1] = np.mean(
            zero_z_face_vertices[:, 1]
        )  # take mean in x
        transformed_vertices = get_transformed_stack_vertices(
            zero_z_face_vertices, views_props, params
        )
        volume = (
            np.min(transformed_vertices, 0),
            np.max(transformed_vertices, 0),
        )  # lower, upper

    elif mode == "union":
        transformed_vertices = get_transformed_stack_vertices(
            stack_vertices, views_props, params
        )
        volume = np.min(transformed_vertices, 0), np.max(
            transformed_vertices, 0
        )

    elif mode == "intersection":
        transformed_vertices = get_transformed_stack_vertices(
            stack_vertices, views_props, params
        )
        volume = np.max(np.min(transformed_vertices, 1), 0), np.min(
            np.max(transformed_vertices, 1), 0
        )

    stack_properties = calc_stack_properties_from_volume(volume, spacing)

    return stack_properties


def combine_stack_props(stack_props_list):
    """
    Combine stack properties from multiple timepoints.

    TODO: This should probably be replaced by simply reusing
    calc_stack_properties_from_view_properties_and_params.

    Parameters
    ----------
    stack_props_list : list of dict

    Returns
    -------
    dict
        Combined stack properties
    """
    combined_stack_props = {}
    combined_stack_props["origin"] = np.min(
        [sp["origin"] for sp in stack_props_list], axis=0
    )
    combined_stack_props["spacing"] = np.min(
        [sp["spacing"] for sp in stack_props_list], axis=0
    )
    combined_stack_props["shape"] = np.max(
        [
            np.ceil(
                (
                    sp["origin"]
                    + sp["shape"] * sp["spacing"]
                    - combined_stack_props["origin"]
                )
                / combined_stack_props["spacing"]
            )
            for sp in stack_props_list
        ],
        axis=0,
    ).astype(np.uint16)

    return combined_stack_props


def get_transformed_stack_vertices(
    stack_keypoints, stack_properties_list, params
):
    ndim = len(stack_properties_list[0]["spacing"])
    vertices = np.zeros(
        (len(stack_properties_list) * len(stack_keypoints), ndim)
    )
    for iim, sp in enumerate(stack_properties_list):
        tmp_vertices = stack_keypoints * np.array(sp["shape"]) * np.array(
            sp["spacing"]
        ) + np.array(sp["origin"])
        inv_params = np.linalg.inv(params[iim])
        tmp_vertices_transformed = (
            np.dot(inv_params[:ndim, :ndim], tmp_vertices.T).T
            + inv_params[:ndim, ndim]
        )
        vertices[
            iim * len(stack_keypoints) : (iim + 1) * len(stack_keypoints)
        ] = tmp_vertices_transformed

    return vertices


def calc_stack_properties_from_volume(volume, spacing):
    """
    :param volume: lower and upper edge of final volume (e.g. [edgeLow,edgeHigh] as calculated by calc_final_stack_cube)
    :param spacing: final spacing
    :return: dictionary containing size, origin and spacing of final stack
    """

    origin = volume[0]
    shape = np.ceil((volume[1] - volume[0]) / spacing).astype(np.uint16)

    properties_dict = {}
    properties_dict["shape"] = shape
    properties_dict["spacing"] = spacing
    properties_dict["origin"] = origin

    return properties_dict


def get_interpolated_image(
    image: np.ndarray,
    mask: np.ndarray,
    method: str = "nearest",
    fill_value: int = 0,
):
    """

    Currently only 2d!

    :param image: a 2D image
    :param mask: a 2D boolean image, True indicates missing values
    :param method: interpolation method, one of
        'nearest', 'linear', 'cubic'.
    :param fill_value: which value to use for filling up data outside the
        convex hull of known pixel values.
        Default is 0, Has no effect for 'nearest'.
    :return: the image with missing values interpolated
    """

    # in case of no known pixels
    if mask.min():
        return image

    from scipy import interpolate

    h, w = image.shape[:2]
    xx, yy = np.meshgrid(np.arange(w), np.arange(h))

    known_x = xx[~mask]
    known_y = yy[~mask]
    known_v = image[~mask]
    missing_x = xx[mask]
    missing_y = yy[mask]

    interp_values = interpolate.griddata(
        (known_x, known_y),
        known_v,
        (missing_x, missing_y),
        method=method,
        fill_value=fill_value,
    )

    interp_image = image.copy()
    interp_image[missing_y, missing_x] = interp_values

    return interp_image
