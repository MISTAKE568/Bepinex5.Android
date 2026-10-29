#ifndef BE5_ASSET_COPY_H
#define BE5_ASSET_COPY_H

#include <android/asset_manager.h>

/* Return 1 for a copied file, 0 for a retained file, and -1 on failure with errno set. */
int be5_copy_asset_file(AAssetManager *manager, const char *source,
                        const char *destination, int preserve_existing);

#endif
