# wrf-era5-runs
A docker image to run WRF over a fixed period of time. 

## Setup
1. The machine must be linux.
2. Docker must be installed on the machine that will run WRF and your user must be part of the "docker" group. Run ```docker run hello-world``` to see if you've set yourself up correctly. Otherwise, follow this procedure: https://docs.docker.com/engine/install/linux-postinstall/
3. Download and extract the WPS_GEOG data via the ```add_geog.sh``` script. This will download and extract the static files in your home directory.
4. Fill out the geogrid section of the namelist.wps. The other sections can be ignored as they will be generated during the preprocessing.
5. Fill out the physics and dynamics sections of the namelist.input. Some parameters in the domains sections can be filled out as well, but most will be filled from the geogrid section from the namelist.wps file.
6. Create and fill out a parameters.toml file based on the parameters_example.toml file.

## Running WRF
Modify the docker-compose.yml file with your namelists and parameters.toml. The docker container will run WPS in the /data folder within the container. You can mount a drive on your machine to more easily see the progress. Once the preprocessing is finished, WRF will be run from the /data/run folder.


