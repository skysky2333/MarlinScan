#include <OpenEXR/ImfChannelList.h>
#include <OpenEXR/ImfChromaticities.h>
#include <OpenEXR/ImfChromaticitiesAttribute.h>
#include <OpenEXR/ImfFrameBuffer.h>
#include <OpenEXR/ImfHeader.h>
#include <OpenEXR/ImfStringAttribute.h>
#include <OpenEXR/ImfTiledOutputFile.h>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace fs = std::filesystem;
namespace exr = OPENEXR_IMF_NAMESPACE;
namespace imath = IMATH_NAMESPACE;

struct Options
{
    fs::path input;
    fs::path output;
    int width = 0;
    int height = 0;
    int tile_size = 256;
    std::string compression = "zip";
    std::string working_space = "linear-rec2020";
    std::string color_encoding = "scene-linear";
    std::string input_order = "rgb";
};

class MappedInput
{
public:
    MappedInput (const fs::path& path, std::uint64_t expected_size)
    {
        if (expected_size > std::numeric_limits<std::size_t>::max ())
            throw std::runtime_error ("Input is too large for this process address space");

        fd_ = ::open (path.c_str (), O_RDONLY);
        if (fd_ < 0) throw std::runtime_error ("Cannot open input backing file: " + path.string ());

        struct stat info
        {};
        if (::fstat (fd_, &info) != 0)
        {
            ::close (fd_);
            fd_ = -1;
            throw std::runtime_error ("Cannot inspect input backing file: " + path.string ());
        }
        if (info.st_size < 0 || static_cast<std::uint64_t> (info.st_size) != expected_size)
        {
            ::close (fd_);
            fd_ = -1;
            throw std::runtime_error (
                "Input backing file size does not match width x height x 3 float32");
        }

        size_ = static_cast<std::size_t> (expected_size);
        data_ = ::mmap (nullptr, size_, PROT_READ, MAP_PRIVATE, fd_, 0);
        if (data_ == MAP_FAILED)
        {
            data_ = nullptr;
            ::close (fd_);
            fd_ = -1;
            throw std::runtime_error ("Cannot memory-map input backing file: " + path.string ());
        }
    }

    MappedInput (const MappedInput&)            = delete;
    MappedInput& operator= (const MappedInput&) = delete;

    ~MappedInput ()
    {
        if (data_ != nullptr) ::munmap (data_, size_);
        if (fd_ >= 0) ::close (fd_);
    }

    const char* data () const { return static_cast<const char*> (data_); }

private:
    int         fd_   = -1;
    void*       data_ = nullptr;
    std::size_t size_ = 0;
};

[[noreturn]] void usage_error (const std::string& message)
{
    throw std::runtime_error (
        message +
        "\nUsage: write_openexr --input INPUT.f32 --output OUTPUT.exr --width PIXELS "
        "--height PIXELS [--tile-size 256] [--compression zip|piz] "
        "[--working-space acescg|linear-rec2020] "
        "[--color-encoding scene-linear|working-linear] [--input-order rgb|bgr]");
}

int parse_positive_int (const std::string& value, const std::string& name)
{
    std::size_t parsed = 0;
    long long   result = 0;
    try
    {
        result = std::stoll (value, &parsed);
    }
    catch (const std::exception&)
    {
        usage_error (name + " must be a positive integer");
    }
    if (parsed != value.size () || result <= 0 || result > std::numeric_limits<int>::max ())
        usage_error (name + " must be a positive integer");
    return static_cast<int> (result);
}

Options parse_options (int argc, char** argv)
{
    Options options;
    for (int i = 1; i < argc; ++i)
    {
        const std::string key = argv[i];
        if (i + 1 >= argc) usage_error ("Missing value for " + key);
        const std::string value = argv[++i];
        if (key == "--input")
            options.input = value;
        else if (key == "--output")
            options.output = value;
        else if (key == "--width")
            options.width = parse_positive_int (value, "--width");
        else if (key == "--height")
            options.height = parse_positive_int (value, "--height");
        else if (key == "--tile-size")
            options.tile_size = parse_positive_int (value, "--tile-size");
        else if (key == "--compression")
            options.compression = value;
        else if (key == "--working-space")
            options.working_space = value;
        else if (key == "--color-encoding")
            options.color_encoding = value;
        else if (key == "--input-order")
            options.input_order = value;
        else
            usage_error ("Unknown option: " + key);
    }

    if (options.input.empty ()) usage_error ("--input is required");
    if (options.output.empty ()) usage_error ("--output is required");
    if (options.width <= 0) usage_error ("--width is required");
    if (options.height <= 0) usage_error ("--height is required");
    if (options.tile_size < 16 || options.tile_size > 2048)
        usage_error ("--tile-size must be between 16 and 2048");
    if (options.compression != "zip" && options.compression != "piz")
        usage_error ("--compression must be zip or piz");
    if (options.working_space != "acescg" && options.working_space != "linear-rec2020")
        usage_error ("--working-space must be acescg or linear-rec2020");
    if (options.color_encoding != "scene-linear" && options.color_encoding != "working-linear")
        usage_error ("--color-encoding must be scene-linear or working-linear");
    if (options.input_order != "rgb" && options.input_order != "bgr")
        usage_error ("--input-order must be rgb or bgr");
    return options;
}

std::uint64_t expected_input_size (int width, int height)
{
    const auto max = std::numeric_limits<std::uint64_t>::max ();
    const auto w   = static_cast<std::uint64_t> (width);
    const auto h   = static_cast<std::uint64_t> (height);
    if (w > max / h || (w * h) > max / (3 * sizeof (float)))
        throw std::runtime_error ("Input dimensions overflow the float32 RGB backing-file size");
    return w * h * 3 * sizeof (float);
}

exr::Chromaticities working_space_chromaticities (const std::string& working_space)
{
    if (working_space == "acescg")
        return exr::Chromaticities (
            imath::V2f (0.713f, 0.293f),
            imath::V2f (0.165f, 0.830f),
            imath::V2f (0.128f, 0.044f),
            imath::V2f (0.32168f, 0.33767f));
    return exr::Chromaticities (
        imath::V2f (0.708f, 0.292f),
        imath::V2f (0.170f, 0.797f),
        imath::V2f (0.131f, 0.046f),
        imath::V2f (0.3127f, 0.3290f));
}

void write_openexr (const Options& options)
{
    const std::uint32_t endian_probe = 1;
    if (*reinterpret_cast<const std::uint8_t*> (&endian_probe) != 1)
        throw std::runtime_error ("Only little-endian float32 backing files are supported");
    if (fs::exists (options.output))
        throw std::runtime_error ("Output already exists: " + options.output.string ());

    fs::path partial = options.output;
    partial += ".partial";
    if (fs::exists (partial))
        throw std::runtime_error ("Partial output already exists: " + partial.string ());

    MappedInput input (options.input, expected_input_size (options.width, options.height));
    try
    {
        exr::Header header (options.width, options.height);
        header.channels ().insert ("R", exr::Channel (exr::FLOAT));
        header.channels ().insert ("G", exr::Channel (exr::FLOAT));
        header.channels ().insert ("B", exr::Channel (exr::FLOAT));
        header.compression () =
            options.compression == "zip" ? exr::ZIP_COMPRESSION : exr::PIZ_COMPRESSION;
        header.setTileDescription (
            exr::TileDescription (
                static_cast<unsigned int> (options.tile_size),
                static_cast<unsigned int> (options.tile_size),
                exr::ONE_LEVEL,
                exr::ROUND_DOWN));
        header.insert (
            "chromaticities",
            exr::ChromaticitiesAttribute (
                working_space_chromaticities (options.working_space)));
        header.insert (
            "marlinScanColorEncoding",
            exr::StringAttribute (options.color_encoding));
        header.insert (
            "marlinScanWorkingSpace",
            exr::StringAttribute (
                options.working_space == "acescg" ? "ACEScg (AP1, D60)"
                                                  : "Linear Rec.2020 (D65)"));
        header.insert ("marlinScanInputPixelType", exr::StringAttribute ("float32"));
        header.insert (
            "marlinScanInputChannelOrder",
            exr::StringAttribute (options.input_order == "rgb" ? "RGB" : "BGR"));
        header.insert ("software", exr::StringAttribute ("MarlinScan"));

        const std::size_t pixel_stride = 3 * sizeof (float);
        const std::size_t row_stride =
            static_cast<std::size_t> (options.width) * pixel_stride;
        const std::size_t red_index   = options.input_order == "rgb" ? 0 : 2;
        const std::size_t green_index = 1;
        const std::size_t blue_index  = options.input_order == "rgb" ? 2 : 0;
        exr::FrameBuffer  frame_buffer;
        frame_buffer.insert (
            "R",
            exr::Slice::Make (
                exr::FLOAT,
                input.data () + red_index * sizeof (float),
                imath::V2i (0, 0),
                options.width,
                options.height,
                pixel_stride,
                row_stride));
        frame_buffer.insert (
            "G",
            exr::Slice::Make (
                exr::FLOAT,
                input.data () + green_index * sizeof (float),
                imath::V2i (0, 0),
                options.width,
                options.height,
                pixel_stride,
                row_stride));
        frame_buffer.insert (
            "B",
            exr::Slice::Make (
                exr::FLOAT,
                input.data () + blue_index * sizeof (float),
                imath::V2i (0, 0),
                options.width,
                options.height,
                pixel_stride,
                row_stride));

        {
            exr::TiledOutputFile output (partial.c_str (), header);
            output.setFrameBuffer (frame_buffer);
            const int tile_rows = output.numYTiles ();
            for (int tile_y = 0; tile_y < tile_rows; ++tile_y)
            {
                output.writeTiles (0, output.numXTiles () - 1, tile_y, tile_y);
                std::cout << "PROGRESS\t" << tile_y + 1 << '\t' << tile_rows << '\n'
                          << std::flush;
            }
        }
        fs::rename (partial, options.output);
    }
    catch (...)
    {
        std::error_code ignored;
        fs::remove (partial, ignored);
        throw;
    }
}

int main (int argc, char** argv)
{
    try
    {
        write_openexr (parse_options (argc, argv));
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "write_openexr: " << error.what () << '\n';
        return 1;
    }
}
