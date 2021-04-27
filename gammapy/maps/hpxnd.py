# Licensed under a 3-clause BSD style license - see LICENSE.rst
import numpy as np
from astropy.io import fits
from astropy.units import Quantity
from astropy.coordinates import SkyCoord
from regions import PointSkyRegion
from gammapy.utils.units import unit_from_fits_image_hdu
from .region import RegionGeom
from .regionnd import RegionNDMap
from .geom import MapCoord, pix_tuple_to_idx
from .hpx import (
    HPX_FITS_CONVENTIONS,
    HpxConv,
    HpxGeom,
    HpxToWcsMapping,
    nside_to_order,
    get_superpixels
)
from .hpxmap import HpxMap
from .utils import INVALID_INDEX

__all__ = ["HpxNDMap"]


class HpxNDMap(HpxMap):
    """HEALPix map with any number of non-spatial dimensions.

    This class uses a N+1D numpy array to represent the sequence of
    HEALPix image planes.  Following the convention of WCS-based maps
    this class uses a column-wise ordering for the data array with the
    spatial dimension being tied to the last index of the array.

    Parameters
    ----------
    geom : `~gammapy.maps.HpxGeom`
        HEALPIX geometry object.
    data : `~numpy.ndarray`
        HEALPIX data array.
        If none then an empty array will be allocated.
    meta : `dict`
        Dictionary to store meta data.
    unit : str or `~astropy.units.Unit`
        The map unit
    """

    def __init__(self, geom, data=None, dtype="float32", meta=None, unit=""):
        data_shape = geom.data_shape

        if data is None:
            data = self._make_default_data(geom, data_shape, dtype)

        super().__init__(geom, data, meta, unit)

    @staticmethod
    def _make_default_data(geom, shape_np, dtype):
        if geom.npix.size > 1:
            data = np.full(shape_np, np.nan, dtype=dtype)
            idx = geom.get_idx(local=True)
            data[idx[::-1]] = 0
        else:
            data = np.zeros(shape_np, dtype=dtype)

        return data

    @classmethod
    def from_wcs_tiles(cls, wcs_tiles, nest=True):
        """Create HEALPix map from WCS tiles.

        Parameters
        ----------
        wcs_tiles : list of  `WcsNDMap`
            Wcs map tiles
        nest : bool
            Whether to use nested HEALPix scheme

        Returns
        -------
        hpx_map : `HpxNDMap`
            HEALPix map
        """
        import healpy as hp

        geom_wcs = wcs_tiles[0].geom

        geom_hpx = HpxGeom.create(
            binsz=geom_wcs.pixel_scales[0],
            frame=geom_wcs.frame,
            nest=nest,
            axes=geom_wcs.axes
        )

        map_hpx = cls.from_geom(geom=geom_hpx, unit=wcs_tiles[0].unit)

        coords = map_hpx.geom.get_coord().skycoord
        nside_superpix = hp.npix2nside(len(wcs_tiles))

        hpx_ref = HpxGeom(nside=nside_superpix, nest=nest, frame=geom_wcs.frame)

        idx = np.arange(map_hpx.geom.to_image().npix)
        indices = get_superpixels(idx, map_hpx.geom.nside, nside_superpix, nest=nest)

        for wcs_tile in wcs_tiles:
            hpx_idx = int(hpx_ref.coord_to_idx(wcs_tile.geom.center_skydir)[0])
            mask = indices == hpx_idx
            map_hpx.data[mask] = wcs_tile.interp_by_coord(coords[mask])

        return map_hpx

    def to_wcs_tiles(self, nside_tiles=4, margin="0 deg", method="nearest", oversampling_factor=1):
        """Convert HpxNDMap to a list of WCS tiles

        Parameters
        ----------
        nside_tiles : int
            Nside for super pixel tiles. Usually nsi
        margin : Angle
            Width margin of the wcs tile
        method : {'nearest', 'linear'}
            Interpolation method
        oversampling_factor : int
            Oversampling factor.

        Returns
        -------
        wcs_tiles : list of `WcsNDMap`
            WCS tiles.
        """
        wcs_tiles = []

        wcs_geoms = self.geom.to_wcs_tiles(
            nside_tiles=nside_tiles, margin=margin
        )

        for geom in wcs_geoms:
            if oversampling_factor > 1:
                geom = geom.upsample(oversampling_factor)

            wcs_map = self.interp_to_geom(geom=geom, method=method)
            wcs_tiles.append(wcs_map)

        return wcs_tiles

    @classmethod
    def from_hdu(cls, hdu, hdu_bands=None, format=None):
        """Make a HpxNDMap object from a FITS HDU.

        Parameters
        ----------
        hdu : `~astropy.io.fits.BinTableHDU`
            The FITS HDU
        hdu_bands  : `~astropy.io.fits.BinTableHDU`
            The BANDS table HDU
        format : str, optional
            FITS convention. If None the format is guessed. The following
            formats are supported:

                - "gadf"
                - "fgst-ccube"
                - "fgst-ltcube"
                - "fgst-bexpcube"
                - "fgst-srcmap"
                - "fgst-template"
                - "fgst-srcmap-sparse"
                - "galprop"
                - "galprop2"

        Returns
        -------
        map : `HpxMap`
            HEALPix map

        """
        if format is None:
            format = HpxConv.identify_hpx_format(hdu.header)

        geom = HpxGeom.from_header(hdu.header, hdu_bands, format=format)

        hpx_conv = HPX_FITS_CONVENTIONS[format]

        shape = geom.axes.shape[::-1]

        # TODO: Should we support extracting slices?

        meta = cls._get_meta_from_header(hdu.header)
        unit = unit_from_fits_image_hdu(hdu.header)
        map_out = cls(geom, None, meta=meta, unit=unit)

        colnames = hdu.columns.names
        cnames = []
        if hdu.header.get("INDXSCHM", None) == "SPARSE":
            pix = hdu.data.field("PIX")
            vals = hdu.data.field("VALUE")
            if "CHANNEL" in hdu.data.columns.names:
                chan = hdu.data.field("CHANNEL")
                chan = np.unravel_index(chan, shape)
                idx = chan + (pix,)
            else:
                idx = (pix,)

            map_out.set_by_idx(idx[::-1], vals)
        else:
            for c in colnames:
                if c.find(hpx_conv.colstring) == 0:
                    cnames.append(c)
            nbin = len(cnames)
            if nbin == 1:
                map_out.data = hdu.data.field(cnames[0])
            else:
                for idx, cname in enumerate(cnames):
                    idx = np.unravel_index(idx, shape)
                    map_out.data[idx + (slice(None),)] = hdu.data.field(cname)

        return map_out

    def to_wcs(
        self,
        sum_bands=False,
        normalize=True,
        proj="AIT",
        oversample=2,
        width_pix=None,
        hpx2wcs=None,
    ):
        from .wcsnd import WcsNDMap

        if sum_bands and self.geom.nside.size > 1:
            map_sum = self.sum_over_axes()
            return map_sum.to_wcs(
                sum_bands=False,
                normalize=normalize,
                proj=proj,
                oversample=oversample,
                width_pix=width_pix,
            )

        # FIXME: Check whether the old mapping is still valid and reuse it
        if hpx2wcs is None:
            geom_wcs_image = self.geom.to_wcs_geom(
                proj=proj, oversample=oversample, width_pix=width_pix
            ).to_image()

            hpx2wcs = HpxToWcsMapping.create(self.geom, geom_wcs_image)

        # FIXME: Need a function to extract a valid shape from npix property

        if sum_bands:
            axes = np.arange(self.data.ndim - 1)
            hpx_data = np.apply_over_axes(np.sum, self.data, axes=axes)
            hpx_data = np.squeeze(hpx_data)
            wcs_shape = tuple([t.flat[0] for t in hpx2wcs.npix])
            wcs_data = np.zeros(wcs_shape).T
            wcs = hpx2wcs.wcs.to_image()
        else:
            hpx_data = self.data
            wcs_shape = tuple([t.flat[0] for t in hpx2wcs.npix]) + self.geom.shape_axes
            wcs_data = np.zeros(wcs_shape).T
            wcs = hpx2wcs.wcs.to_cube(self.geom.axes)

        # FIXME: Should reimplement instantiating map first and fill data array
        hpx2wcs.fill_wcs_map_from_hpx_data(hpx_data, wcs_data, normalize)
        return WcsNDMap(wcs, wcs_data, unit=self.unit)

    def _pad_spatial(self, pad_width, mode="constant", cval=0):
        geom = self.geom._pad_spatial(pad_width=pad_width)
        map_out = self._init_copy(geom=geom, data=None)
        map_out.coadd(self)
        coords = geom.get_coord(flat=True)
        m = self.geom.contains(coords)
        coords = tuple([c[~m] for c in coords])

        if mode == "constant":
            map_out.set_by_coord(coords, cval)
        elif mode == "interp":
            raise ValueError("Method 'interp' not supported for HpxMap")
        else:
            raise ValueError(f"Unrecognized pad mode: {mode!r}")

        return map_out

    def crop(self, crop_width):
        geom = self.geom.crop(crop_width)
        map_out = self._init_copy(geom=geom, data=None)
        map_out.coadd(self)
        return map_out

    def upsample(self, factor, preserve_counts=True):
        geom = self.geom.upsample(factor)
        coords = geom.get_coord()
        data = self.get_by_coord(coords)

        if preserve_counts:
            data /= factor ** 2

        return self._init_copy(geom=geom, data=data)

    def downsample(self, factor, preserve_counts=True):
        geom = self.geom.downsample(factor)
        coords = self.geom.get_coord()
        vals = self.get_by_coord(coords)

        map_out = self._init_copy(geom=geom, data=None)
        map_out.fill_by_coord(coords, vals)

        if not preserve_counts:
            map_out.data /= factor ** 2

        return map_out

    def interp_by_coord(self, coords, method="linear"):
        # inherited docstring
        coords = MapCoord.create(coords, frame=self.geom.frame)

        if method == "linear":
            return self._interp_by_coord(coords)
        elif method == "nearest":
            return self.get_by_coord(coords)
        else:
            raise ValueError(f"Invalid interpolation method: {method!r}")

    def interp_by_pix(self, pix, method=None):
        """Interpolate map values at the given pixel coordinates.
        """
        raise NotImplementedError

    def cutout(self, position, width, *args, **kwargs):
        """Create a cutout around a given position.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Center position of the cutout region.
        width : `~astropy.coordinates.Angle` or `~astropy.units.Quantity`
            Radius of the circular cutout region.

        Returns
        -------
        cutout : `~gammapy.maps.HpxNDMap`
            Cutout map
        """
        geom = self.geom.cutout(position=position, width=width)

        if self.geom.is_allsky:
            idx = geom._ipix
        else:
            idx = self.geom.to_image().global_to_local((geom._ipix,))

        data = self.data[..., idx]
        return self.__class__(
            geom=geom, data=data, unit=self.unit, meta=self.meta
        )

    def stack(self, other, weights=None):
        """Stack cutout into map.

        Parameters
        ----------
        other : `HpxNDMap`
            Other map to stack
        weights : `HpxNDMap`
            Array to be used as weights. The spatial geometry must be equivalent
            to `other` and additional axes must be broadcastable.
        """
        if self.geom == other.geom:
            idx = None
        elif self.geom.is_aligned(other.geom):
            if self.geom.is_allsky:
                idx = other.geom._ipix
            else:
                idx = self.geom.to_image().global_to_local((other.geom._ipix,))[0]
        else:
            raise ValueError(
                "Can only stack equivalent maps or cutout of the same map."
            )

        data = other.quantity.to_value(self.unit)

        if weights is not None:
            if not other.geom.to_image() == weights.geom.to_image():
                raise ValueError("Incompatible spatial geoms between map and weights")
            data = data * weights.data

        if idx is None:
            self.data += data
        else:
            self.data[..., idx] += data

    def smooth(self, width, kernel="gauss"):
        """Smooth the map.

        Iterates over 2D image planes, processing one at a time.

        Parameters
        ----------
        width : `~astropy.units.Quantity`, str or float
            Smoothing width given as quantity or float. If a float is given it
            interpreted as smoothing width in pixels. If an (angular) quantity
            is given it converted to pixels using ``healpy.nside2resol``.
            It corresponds to the standard deviation in case of a Gaussian kernel,
            and the radius in case of a disk kernel.
        kernel : {'gauss', 'disk'}
            Kernel shape

        Returns
        -------
        image : `HpxNDMap`
            Smoothed image (a copy, the original object is unchanged).
        """
        import healpy as hp

        if not self.geom.is_allsky:
            raise NotImplementedError("Smoothing is only possible for all-sky maps")

        nside = self.geom.nside
        lmax = 3*nside-1 # maximum l of the power spectrum
        nest = self.geom.nest

        # The smoothing width is expected by healpy in radians
        if isinstance(width, (Quantity, str)):
            width = Quantity(width)
            width = width.to_value("rad")
        else:
            binsz = np.degrees(hp.nside2resol(nside))
            width = width * binsz
            width = np.deg2rad(width)

        smoothed_data = np.empty(self.data.shape, dtype=float)

        for img, idx in self.iter_by_image():
            img = img.astype(float)

            if nest:
                # reorder to ring to do the smoothing
                img = hp.pixelfunc.reorder(img, n2r=True)

            if kernel == "gauss":
                data = hp.sphtfunc.smoothing(img, sigma=width, pol=False, verbose=False, lmax=lmax)
            elif kernel == "disk":
                # create the step function in angular space
                theta = np.linspace(0,width)
                beam = np.ones(len(theta))
                beam[theta>width]=0
                # convert to the spherical harmonics space
                window_beam = hp.sphtfunc.beam2bl(beam, theta, lmax)
                # normalize the window beam
                window_beam = window_beam/window_beam.max()
                data = hp.sphtfunc.smoothing(img, beam_window=window_beam, pol=False, verbose=False, lmax=lmax)
            else:
                raise ValueError(f"Invalid kernel: {kernel!r}")

            if nest:
                # reorder back to nest after the smoothing
                data = hp.pixelfunc.reorder(data, r2n=True)
            smoothed_data[idx] = data

        return self._init_copy(data=smoothed_data)

    def get_by_idx(self, idx):
        # inherited docstring
        idx = pix_tuple_to_idx(idx)
        idx = self.geom.global_to_local(idx)
        return self.data.T[idx]

    def _interp_by_coord(self, coords):
        """Linearly interpolate map values."""
        pix, wts = self.geom.interp_weights(coords)

        if self.geom.is_image:
            return np.sum(self.data.T[tuple(pix)] * wts, axis=0)

        val = np.zeros(pix[0].shape[1:])

        # Loop over function values at corners
        for i in range(2 ** len(self.geom.axes)):
            pix_i = []
            wt = np.ones(pix[0].shape[1:])[np.newaxis, ...]
            for j, ax in enumerate(self.geom.axes):
                idx = ax.coord_to_idx(coords[ax.name])
                idx = np.clip(idx, 0, len(ax.center) - 2)

                w = ax.center[idx + 1] - ax.center[idx]
                c = Quantity(coords[ax.name], ax.center.unit, copy=False).value

                if i & (1 << j):
                    wt *= (c - ax.center[idx].value) / w.value
                    pix_i += [idx + 1]
                else:
                    wt *= 1.0 - (c - ax.center[idx].value) / w.value
                    pix_i += [idx]

            if not self.geom.is_regular:
                pix, wts = self.geom.interp_weights(coords, idxs=pix_i)

            wts[pix[0] == INVALID_INDEX.int] = 0
            wt[~np.isfinite(wt)] = 0
            val += np.nansum(wts * wt * self.data.T[tuple(pix[:1] + pix_i)], axis=0)

        return val

    def fill_by_idx(self, idx, weights=None):
        idx = pix_tuple_to_idx(idx)
        msk = np.all(np.stack([t != INVALID_INDEX.int for t in idx]), axis=0)
        if weights is not None:
            weights = weights[msk]
        idx = [t[msk] for t in idx]

        idx_local = list(self.geom.global_to_local(idx))
        msk = idx_local[0] >= 0
        idx_local = [t[msk] for t in idx_local]
        if weights is not None:
            if isinstance(weights, Quantity):
                weights = weights.to_value(self.unit)
            weights = weights[msk]

        idx_local = np.ravel_multi_index(idx_local, self.data.T.shape)
        idx_local, idx_inv = np.unique(idx_local, return_inverse=True)
        weights = np.bincount(idx_inv, weights=weights)
        self.data.T.flat[idx_local] += weights

    def set_by_idx(self, idx, vals):
        idx = pix_tuple_to_idx(idx)
        idx_local = self.geom.global_to_local(idx)
        self.data.T[idx_local] = vals

    def _make_cols(self, header, conv):
        shape = self.data.shape
        cols = []

        if header["INDXSCHM"] == "SPARSE":
            data = self.data.copy()
            data[~np.isfinite(data)] = 0
            nonzero = np.where(data > 0)
            value = data[nonzero].astype(float)
            pix = self.geom.local_to_global(nonzero[::-1])[0]
            if len(shape) == 1:
                cols.append(fits.Column("PIX", "J", array=pix))
                cols.append(fits.Column("VALUE", "E", array=value))
            else:
                channel = np.ravel_multi_index(nonzero[:-1], shape[:-1])
                cols.append(fits.Column("PIX", "J", array=pix))
                cols.append(fits.Column("CHANNEL", "I", array=channel))
                cols.append(fits.Column("VALUE", "E", array=value))
        elif len(shape) == 1:
            name = conv.colname(indx=conv.firstcol)
            array = self.data.astype(float)
            cols.append(fits.Column(name, "E", array=array))
        else:
            for i, idx in enumerate(np.ndindex(shape[:-1])):
                name = conv.colname(indx=i + conv.firstcol)
                array = self.data[idx].astype(float)
                cols.append(fits.Column(name, "E", array=array))

        return cols

    def to_swapped(self):
        import healpy as hp

        hpx_out = self.geom.to_swapped()
        map_out = self._init_copy(geom=hpx_out, data=None)
        idx = self.geom.get_idx(flat=True)
        vals = self.get_by_idx(idx)
        if self.geom.nside.size > 1:
            nside = self.geom.nside[idx[1:]]
        else:
            nside = self.geom.nside

        if self.geom.nest:
            idx_new = tuple([hp.nest2ring(nside, idx[0])]) + idx[1:]
        else:
            idx_new = tuple([hp.ring2nest(nside, idx[0])]) + idx[1:]

        map_out.set_by_idx(idx_new, vals)
        return map_out

    def to_region_nd_map(self, region, func=np.nansum, weights=None, method="nearest"):
        """Get region ND map in a given region.

        By default the whole map region is considered.

        Parameters
        ----------
        region: `~regions.Region` or `~astropy.coordinates.SkyCoord`
             Region.
        func : numpy.func
            Function to reduce the data. Default is np.nansum.
            For boolean Map, use np.any or np.all.
        weights : `WcsNDMap`
            Array to be used as weights. The geometry must be equivalent.
        method : {"nearest", "linear"}
            How to interpolate if a position is given.

        Returns
        -------
        spectrum : `~gammapy.maps.RegionNDMap`
            Spectrum in the given region.
        """
        if isinstance(region, SkyCoord):
            region = PointSkyRegion(region)

        if weights is not None:
            if not self.geom == weights.geom:
                raise ValueError("Incompatible spatial geoms between map and weights")

        geom = RegionGeom(region=region, axes=self.geom.axes)

        if isinstance(region, PointSkyRegion):
            coords = geom.get_coord()
            data = self.interp_by_coord(coords=coords, method=method)
            if weights is not None:
                data *= weights.interp_by_coord(coords=coords, method=method)
        else:
            cutout = self.cutout(position=geom.center_skydir, width=np.max(geom.width))

            if weights is not None:
                weights_cutout = weights.cutout(
                    position=geom.center_skydir, width=geom.width
                )
                cutout.data *= weights_cutout.data

            mask = cutout.geom.to_image().region_mask([region]).data
            data = func(cutout.data[..., mask], axis=-1)

        return RegionNDMap(geom=geom, data=data, unit=self.unit, meta=self.meta.copy())

    def plot(
        self,
        method="raster",
        ax=None,
        normalize=False,
        proj="AIT",
        oversample=2,
        width_pix=1000,
        **kwargs,
    ):
        """Quickplot method.

        This will generate a visualization of the map by converting to
        a rasterized WCS image (method='raster') or drawing polygons
        for each pixel (method='poly').

        Parameters
        ----------
        method : {'raster','poly'}
            Method for mapping HEALPix pixels to a two-dimensional
            image.  Can be set to 'raster' (rasterization to cartesian
            image plane) or 'poly' (explicit polygons for each pixel).
            WARNING: The 'poly' method is much slower than 'raster'
            and only suitable for maps with less than ~10k pixels.
        proj : string, optional
            Any valid WCS projection type.
        oversample : float
            Oversampling factor for WCS map. This will be the
            approximate ratio of the width of a HPX pixel to a WCS
            pixel. If this parameter is None then the width will be
            set from ``width_pix``.
        width_pix : int
            Width of the WCS geometry in pixels.  The pixel size will
            be set to the number of pixels satisfying ``oversample``
            or ``width_pix`` whichever is smaller.  If this parameter
            is None then the width will be set from ``oversample``.
        **kwargs : dict
            Keyword arguments passed to `~matplotlib.pyplot.imshow`.

        Returns
        -------
        fig : `~matplotlib.figure.Figure`
            Figure object.
        ax : `~astropy.visualization.wcsaxes.WCSAxes`
            WCS axis object
        im : `~matplotlib.image.AxesImage` or `~matplotlib.collections.PatchCollection`
            Image object.

        """
        if method == "raster":
            m = self.to_wcs(
                sum_bands=True,
                normalize=normalize,
                proj=proj,
                oversample=oversample,
                width_pix=width_pix,
            )
            return m.plot(ax, **kwargs)
        elif method == "poly":
            return self._plot_poly(proj=proj, ax=ax)
        else:
            raise ValueError(f"Invalid method: {method!r}")

    def _plot_poly(self, proj="AIT", step=1, ax=None):
        """Plot the map using a collection of polygons.

        Parameters
        ----------
        proj : string, optional
            Any valid WCS projection type.
        step : int
            Set the number vertices that will be computed for each
            pixel in multiples of 4.
        """
        # FIXME: At the moment this only works for all-sky maps if the
        # projection is centered at (0,0)

        # FIXME: Figure out how to force a square aspect-ratio like imshow

        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon
        from matplotlib.collections import PatchCollection
        import healpy as hp

        wcs = self.geom.to_wcs_geom(proj=proj, oversample=1)
        if ax is None:
            fig = plt.gcf()
            ax = fig.add_subplot(111, projection=wcs.wcs, aspect="equal")

        wcs_lonlat = wcs.center_coord[:2]
        idx = self.geom.get_idx()
        vtx = hp.boundaries(self.geom.nside, idx[0], nest=self.geom.nest, step=step)
        theta, phi = hp.vec2ang(np.rollaxis(vtx, 2))
        theta = theta.reshape((4 * step, -1)).T
        phi = phi.reshape((4 * step, -1)).T

        patches = []
        data = []

        def get_angle(x, t):
            return 180.0 - (180.0 - x + t) % 360.0

        for i, (x, y) in enumerate(zip(phi, theta)):

            lon, lat = np.degrees(x), np.degrees(np.pi / 2.0 - y)
            # Add a small ofset to avoid vertices wrapping to the
            # other size of the projection
            if get_angle(np.median(lon), wcs_lonlat[0].to_value("deg")) > 0:
                idx = wcs.coord_to_pix((lon - 1e-4, lat))
            else:
                idx = wcs.coord_to_pix((lon + 1e-4, lat))

            dist = np.max(np.abs(idx[0][0] - idx[0]))

            # Split pixels that wrap around the edges of the projection
            if dist > wcs.npix[0] / 1.5:
                lon, lat = np.degrees(x), np.degrees(np.pi / 2.0 - y)
                lon0 = lon - 1e-4
                lon1 = lon + 1e-4
                pix0 = wcs.coord_to_pix((lon0, lat))
                pix1 = wcs.coord_to_pix((lon1, lat))

                idx0 = np.argsort(pix0[0])
                idx1 = np.argsort(pix1[0])

                pix0 = (pix0[0][idx0][:3], pix0[1][idx0][:3])
                pix1 = (pix1[0][idx1][1:], pix1[1][idx1][1:])

                patches.append(Polygon(np.vstack((pix0[0], pix0[1])).T, True))
                patches.append(Polygon(np.vstack((pix1[0], pix1[1])).T, True))
                data.append(self.data[i])
                data.append(self.data[i])
            else:
                polygon = Polygon(np.vstack((idx[0], idx[1])).T, True)
                patches.append(polygon)
                data.append(self.data[i])

        p = PatchCollection(patches, linewidths=0, edgecolors="None")
        p.set_array(np.array(data))
        ax.add_collection(p)
        ax.autoscale_view()
        ax.coords.grid(color="w", linestyle=":", linewidth=0.5)

        return fig, ax, p
