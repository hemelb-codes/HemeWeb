import os
import subprocess
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.core.validators import MaxValueValidator
from django.db import models
from django.db import transaction

from django_rq import job


def job_directory_path(instance, filename):
    """ Function to generate the correct upload path folder
        Every job should have its own folder
        <upload_dir>/<hex-id-of-job>/<filename>
    """
    return os.path.join(settings.JOB_FILES_UPLOAD_DIR,
                        instance.id.hex,
                        'inputs',
                        filename)


@job
def run_setup(job_instance):
    """ Function to run the setup tools on HemeLB scripts to convert the profile file
        and geometry file in form of .stl and .pr2 into the input for the job execution
        which are .xml and .gmy
    """
    command = "/var/src/hemelb/virtualenv/bin/python \
    /var/src/hemelb/Tools/setuptool/scripts/hemelb-setup-nogui \
    --stl {} {} ".format(job_instance.stl_file.name, job_instance.profile_file.name)

    completed = subprocess.call(command, shell=True)

    if completed == 0:
        # Assign generated .gmy and .xml to the correct fields
        job_instance.configuration_file.name = job_instance.stl_file.name.replace('.stl', '.xml')
        job_instance.input_file.name = job_instance.stl_file.name.replace('.stl', '.gmy')
        job_instance.status = job_instance.ADDED
    else:
        job_instance.status = job_instance.FAILED

    job_instance.save()


@job
def run_job(job_instance):
        """ Function to be called in the background to run the submitted job
            This function will compose the correct command for the job
            and make sure that the stdout and stderr of the command is correctly
            piped into the correct files
        """

        command = "export ANSIBLE_HOST_KEY_CHECKING=False; \
        export AWS_SECRET_ACCESS_KEY={}; \
        export AWS_ACCESS_KEY_ID={}; \
        /var/www/hemeweb/virtualenv/bin/ansible-playbook \
        -u ubuntu \
        --extra-vars 'image={} master_ip={} worker_node_count={}  \
        instance_tags={} input={} output={} \
        worker_instance_type={} log_file={} core_count={} container_image={}' \
        jobs/scripts/aws_ec2.yml\
        ".format(
            settings.AWS_SECRET_ACCESS_KEY,
            settings.AWS_ACCESS_KEY_ID,
            settings.HEMEWEB_IMAGE_ID,
            settings.HOST_IP,
            job_instance.instance_count,
            "job-{}".format(job_instance.id),
            job_instance.configuration_file.name,
            job_instance.get_result_directory_path(),
            job_instance.get_instance_id(),
            job_instance.get_log_file_path('hemelb'),
            job_instance.get_core_count(),
            job_instance.get_container_image_display(),
        )

        with open(job_instance.get_log_file_path('stdout'), 'w') as stdout_file:
            with open(job_instance.get_log_file_path('stderr'), 'w') as stderr_file:
                # Update the status first
                with transaction.atomic():
                    job_instance.status = job_instance.RUNNING
                    job_instance.save(update_fields=['status'])

                # Run long running job
                completed = subprocess.call(command,
                                            stdout=stdout_file,
                                            stderr=stderr_file,
                                            shell=True)

                # Update the status of job accordingly
                if completed != 0:
                    job_instance.status = job_instance.FAILED
                    job_instance.save(update_fields=['status'])
                    return

        # Generate VTU
        command = "/var/src/hemelb/virtualenv/bin/python \
        /var/src/hemelb/Tools/hemeTools/converters/GmyUnstructuredGridReader.py \
        {} {} ".format(job_instance.configuration_file.name,
                       job_instance.get_output_path())

        completed = subprocess.call(command, shell=True)
        if completed == 0:
            job_instance.status = job_instance.FAILED
            job_instance.save(update_fields=['status'])
            return

        # Combine VTU with the Extracted image
        command = "/var/src/hemelb/virtualenv/bin/python \
        /var/src/hemelb/Tools/hemeTools/converters/ExtractedPropertyUnstructuredGridReader.py \
        {} {} ".format(job_instance.get_output_path(),
                       job_instance.get_result_extracted_directory_path())

        completed = subprocess.call(command, shell=True)
        if completed == 0:
            job_instance.status = job_instance.FAILED
            job_instance.save(update_fields=['status'])
            return

        job_instance.status = job_instance.DONE
        job_instance.save()


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    configuration_file = models.FileField(upload_to=job_directory_path,
                                          verbose_name="Config (.xml)",
                                          blank=True)
    input_file = models.FileField(upload_to=job_directory_path,
                                  verbose_name="Input (.gmy)",
                                  blank=True)

    stl_file = models.FileField(upload_to=job_directory_path,
                                verbose_name="Geometry file (.stl)",
                                blank=True)
    profile_file = models.FileField(upload_to=job_directory_path,
                                    verbose_name="Profile file (.pr2)",
                                    blank=True)
    output_file = models.FileField(upload_to=job_directory_path,
                                   blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    CONTAINER_CHOICES = (
        (1, 'seiryuz/hemelb-core:0.0.2'),
        (2, 'seiryuz/hemelb-core:0.0.3'),
    )
    container_image = models.IntegerField(choices=CONTAINER_CHOICES, default=1)

    INSTANCE_CHOICES = (
        (2, '2 Cores'),
        (4, '4 Cores'),
        (8, '8 Cores'),
        (16, '16 Cores'),
    )
    instance_type = models.IntegerField(choices=INSTANCE_CHOICES, default=2)
    instance_count = models.IntegerField(default=1, validators=[MaxValueValidator(36)])

    ADDED = 1
    QUEUED = 2
    RUNNING = 10
    DONE = 3
    PREPROCESSING = 4
    FAILED = 0
    STATUS_CHOICES = (
        (ADDED, 'Added'),
        (QUEUED, 'Queued'),
        (RUNNING, 'Running'),
        (DONE, 'Done'),
        (FAILED, 'Failed'),
        (PREPROCESSING, 'Preprocessing'),
    )
    status = models.IntegerField(choices=STATUS_CHOICES, default=ADDED)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return self.id.hex

    def get_absolute_url(self):
        return reverse('jobs:details', kwargs={"pk": str(self.id)})

    def get_next_step_url(self):
        if self.status == self.ADDED:
            return reverse('jobs:configure1', kwargs={"pk": str(self.id)})
        if self.status == self.PREPROCESSING:
            return reverse('jobs:preprocessing', kwargs={"pk": str(self.id)})
        else:
            return self.get_absolute_url()

    def get_job_directory_path(self):
        return os.path.join(settings.JOB_FILES_UPLOAD_DIR,
                            self.id.hex)

    def get_input_directory_path(self):
        return os.path.join(self.get_job_directory_path(),
                            'inputs')

    def get_log_directory_path(self):
        return os.path.join(self.get_job_directory_path(),
                            'logs')

    def get_result_directory_path(self):
        return os.path.join(self.get_job_directory_path(),
                            'result')

    def get_result_extracted_directory_path(self):
        return os.path.join(self.get_result_extracted_directory_path(),
                            'Extracted/*')

    def get_output_path(self):
        return os.path.join(self.get_result_directory_path(),
                            '{}.vtu'.format(str(self.id)))

    def get_packaged_output_path(self):
        return os.path.join(self.get_result_directory_path(),
                            '{}.tar.gz'.format(str(self.id)))

    def get_log_file_path(self, log_type):
        return os.path.join(self.get_log_directory_path(),
                            log_type)

    def get_container_image_url(self):
        container_string = self.get_container_image_display().split(':')[0]
        return "http://hub.docker.com/r/{}".format(container_string)

    def get_instance_id(self):
        if int(self.instance_type) == 2:
            return 'c4.large'
        elif int(self.instance_type) == 4:
            return 'c4.xlarge'
        elif int(self.instance_type) == 8:
            return 'c4.2xlarge'
        elif int(self.instance_type) == 16:
            return 'c4.4xlarge'

    def get_core_count(self):
        return int(self.instance_count) * int(self.instance_type)

    def get_output(self, log_type):
        key = "{}:log:{}".format(self.id.hex, log_type)

        # Hit the cache first
        output = cache.get(key)

        # Cache Miss
        if output is None:

            # Read from the file, this is slow
            with open(self.get_log_file_path(log_type), 'r') as _file:
                output = _file.read()

            # Save it to the cache, depending on the likelihood of the content
            # being updated
            if self.status != self.DONE and self.status != self.FAILED:
                cache.set(key, output, timeout=5)
            else:
                cache.set(key, output, timeout=5000)

        return output

    def prepare_directories(self):
        os.makedirs(self.get_input_directory_path())
        os.makedirs(self.get_log_directory_path())
        open(self.get_log_file_path('stdout'), 'a').close()
        open(self.get_log_file_path('stderr'), 'a').close()
        open(self.get_log_file_path('hemelb'), 'a').close()

    def package_output(self):
        # Combine VTU with the Extracted image
        command = "tar -czf {} {} ".format(self.get_packaged_output_path(),
                                      self.get_result_extracted_directory_path())

        subprocess.call(command, shell=True)
        self.output_file.name = self.get_packaged_output_path()
        self.save()

    def enqueue_job(self, async=True):
        """ function to queue job execution to background worker.
        """
        if async:
            run_job.delay(self)
        else:
            run_job(self)

    def enqueue_setup(self, async=True):
        """ function to queue job execution to background worker.
        """
        if async:
            run_setup.delay(self)
        else:
            run_setup(self)
