import numpy as np
from PIL import Image
from pydantic import BaseModel

from invokeai.app.invocations.baseinvocation import (
    BaseInvocation,
    BaseInvocationOutput,
    InputField,
    InvocationContext,
    OutputField,
    WithMetadata,
    WithWorkflow,
    invocation,
    invocation_output,
)
from invokeai.app.invocations.primitives import ImageField, ImageOutput
from invokeai.app.services.image_records.image_records_common import ImageCategory, ResourceOrigin
from invokeai.backend.tiles.tiles import calc_tiles_with_overlap, merge_tiles_with_linear_blending
from invokeai.backend.tiles.utils import Tile


class TileWithImage(BaseModel):
    tile: Tile
    image: ImageField


@invocation_output("calculate_image_tiles_output")
class CalculateImageTilesOutput(BaseInvocationOutput):
    tiles: list[Tile] = OutputField(description="The tiles coordinates that cover a particular image shape.")


@invocation("calculate_image_tiles", title="Calculate Image Tiles", tags=["tiles"], category="tiles", version="1.0.0")
class CalculateImageTilesInvocation(BaseInvocation):
    """Calculate the coordinates and overlaps of tiles that cover a target image shape."""

    image_width: int = InputField(ge=1, default=1024, description="The image width, in pixels, to calculate tiles for.")
    image_height: int = InputField(
        ge=1, default=1024, description="The image height, in pixels, to calculate tiles for."
    )
    tile_width: int = InputField(ge=1, default=576, description="The tile width, in pixels.")
    tile_height: int = InputField(ge=1, default=576, description="The tile height, in pixels.")
    overlap: int = InputField(
        ge=0,
        default=128,
        description="The target overlap, in pixels, between adjacent tiles. Adjacent tiles will overlap by at least this amount",
    )

    def invoke(self, context: InvocationContext) -> CalculateImageTilesOutput:
        tiles = calc_tiles_with_overlap(
            image_height=self.image_height,
            image_width=self.image_width,
            tile_height=self.tile_height,
            tile_width=self.tile_width,
            overlap=self.overlap,
        )
        return CalculateImageTilesOutput(tiles=tiles)


@invocation_output("tile_to_properties_output")
class TileToPropertiesOutput(BaseInvocationOutput):
    coords_top: int = OutputField(description="Top coordinate of the tile relative to its parent image.")
    coords_bottom: int = OutputField(description="Bottom coordinate of the tile relative to its parent image.")
    coords_left: int = OutputField(description="Left coordinate of the tile relative to its parent image.")
    coords_right: int = OutputField(description="Right coordinate of the tile relative to its parent image.")

    overlap_top: int = OutputField(description="Overlap between this tile and its top neighbor.")
    overlap_bottom: int = OutputField(description="Overlap between this tile and its bottom neighbor.")
    overlap_left: int = OutputField(description="Overlap between this tile and its left neighbor.")
    overlap_right: int = OutputField(description="Overlap between this tile and its right neighbor.")


@invocation("tile_to_properties", title="Tile to Properties", tags=["tiles"], category="tiles", version="1.0.0")
class TileToPropertiesInvocation(BaseInvocation):
    """Split a Tile into its individual properties."""

    tile: Tile = InputField(description="The tile to split into properties.")

    def invoke(self, context: InvocationContext) -> TileToPropertiesOutput:
        return TileToPropertiesOutput(
            coords_top=self.tile.coords.top,
            coords_bottom=self.tile.coords.bottom,
            coords_left=self.tile.coords.left,
            coords_right=self.tile.coords.right,
            overlap_top=self.tile.overlap.top,
            overlap_bottom=self.tile.overlap.bottom,
            overlap_left=self.tile.overlap.left,
            overlap_right=self.tile.overlap.right,
        )


@invocation_output("pair_tile_image_output")
class PairTileImageOutput(BaseInvocationOutput):
    tile_with_image: TileWithImage = OutputField(description="A tile description with its corresponding image.")


@invocation("pair_tile_image", title="Pair Tile with Image", tags=["tiles"], category="tiles", version="1.0.0")
class PairTileImageInvocation(BaseInvocation):
    """Pair an image with its tile properties."""

    # TODO(ryand): The only reason that PairTileImage is needed is because the iterate/collect nodes don't preserve
    # order. Can this be fixed?

    image: ImageField = InputField(description="The tile image.")
    tile: Tile = InputField(description="The tile properties.")

    def invoke(self, context: InvocationContext) -> PairTileImageOutput:
        return PairTileImageOutput(
            tile_with_image=TileWithImage(
                tile=self.tile,
                image=self.image,
            )
        )


@invocation("merge_tiles_to_image", title="Merge Tiles to Image", tags=["tiles"], category="tiles", version="1.0.0")
class MergeTilesToImageInvocation(BaseInvocation, WithMetadata, WithWorkflow):
    """Merge multiple tile images into a single image."""

    # Inputs
    image_width: int = InputField(ge=1, description="The width of the output image, in pixels.")
    image_height: int = InputField(ge=1, description="The height of the output image, in pixels.")
    tiles_with_images: list[TileWithImage] = InputField(description="A list of tile images with tile properties.")
    blend_amount: int = InputField(
        ge=0,
        description="The amount to blend adjacent tiles in pixels. Must be <= the amount of overlap between adjacent tiles.",
    )

    def invoke(self, context: InvocationContext) -> ImageOutput:
        images = [twi.image for twi in self.tiles_with_images]
        tiles = [twi.tile for twi in self.tiles_with_images]

        # Get all tile images for processing.
        # TODO(ryand): It pains me that we spend time PNG decoding each tile from disk when they almost certainly
        # existed in memory at an earlier point in the graph.
        tile_np_images: list[np.ndarray] = []
        for image in images:
            pil_image = context.services.images.get_pil_image(image.image_name)
            pil_image = pil_image.convert("RGB")
            tile_np_images.append(np.array(pil_image))

        # Prepare the output image buffer.
        # Check the first tile to determine how many image channels are expected in the output.
        channels = tile_np_images[0].shape[-1]
        dtype = tile_np_images[0].dtype
        np_image = np.zeros(shape=(self.image_height, self.image_width, channels), dtype=dtype)

        merge_tiles_with_linear_blending(
            dst_image=np_image, tiles=tiles, tile_images=tile_np_images, blend_amount=self.blend_amount
        )
        pil_image = Image.fromarray(np_image)

        image_dto = context.services.images.create(
            image=pil_image,
            image_origin=ResourceOrigin.INTERNAL,
            image_category=ImageCategory.GENERAL,
            node_id=self.id,
            session_id=context.graph_execution_state_id,
            is_intermediate=self.is_intermediate,
            metadata=self.metadata,
            workflow=self.workflow,
        )
        return ImageOutput(
            image=ImageField(image_name=image_dto.image_name),
            width=image_dto.width,
            height=image_dto.height,
        )